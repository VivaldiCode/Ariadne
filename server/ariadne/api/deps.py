from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ariadne.auth import find_principal
from ariadne.db.models import Principal
from ariadne.services import Services

bearer = HTTPBearer(auto_error=False, description="Ariadne API key")


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    services.sync_config()  # o cofre mudou noutro processo? (cache curta, barato)
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def require_principal(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None:
        raise _unauthorized("Missing API key (Authorization: Bearer …)")
    principal = find_principal(session, credentials.credentials)
    if principal is None:
        raise _unauthorized("Invalid API key")
    if principal.scope != "full":
        # As chaves dos clientes externos (âmbito `mcp`) não chegam a nenhuma rota da API REST.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key only grants access to MCP (/mcp).",
        )
    return principal


PrincipalDep = Annotated[Principal, Depends(require_principal)]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _route(request: Request) -> str:
    """`MÉTODO /caminho` do pedido, para a mensagem do 403 dizer o que foi recusado."""
    return f"{request.method} {request.url.path}"


def require_user(
    request: Request, principal: Annotated[Principal, Depends(require_principal)]
) -> Principal:
    """Rotas de pessoas (o painel, as estatísticas, as escritas): um agente lê pelo MCP."""
    if principal.kind != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{_route(request)} is only for 'user' principals (people), not agents.",
        )
    return principal


UserDep = Annotated[Principal, Depends(require_user)]


def require_admin(
    request: Request, principal: Annotated[Principal, Depends(require_user)]
) -> Principal:
    """Principais e chaves, e todas as escritas (computação, curadoria, domínios, coleções, …), são
    só de administradores: um `user` `member` só lê."""
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{_route(request)} requires an administrator; members have read-only access.",
        )
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]
