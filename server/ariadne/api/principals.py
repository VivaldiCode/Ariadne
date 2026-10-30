"""Principais e chaves de API na API REST (STRUCTURE.md, secção 11): só administradores.

A chave mostra-se uma única vez, na resposta de `POST`; na base de dados fica só o hash. Revogar
apaga o hash (a chave deixa de funcionar) e mantém o principal, para o histórico das chamadas
(`tool_call`, `query_log`) continuar a apontar para alguém. Apagar (`POST /{id}/erase`) é só para
os clientes externos (chaves `mcp`) e leva o principal e o histórico todo (RGPD).

Para os clientes externos há ainda: suspender e retomar (a chave mantém o hash, por isso o mesmo
valor volta a funcionar), a quota mensal de pedidos, a criação idempotente (`external_id` ou o
cabeçalho `Idempotency-Key`) e o uso por chave. As chaves `mcp` nunca chegam a estas rotas
(`require_principal` recusa-as com 403), nem as de agentes ou de membros (`AdminDep`).
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ariadne.api.deps import AdminDep, SessionDep
from ariadne.auth import PrincipalKind, Role, Scope, create_principal
from ariadne.calls import tracked_call
from ariadne.client_usage import DEFAULT_USAGE_DAYS, ClientUsage, usage
from ariadne.db.models import Principal
from ariadne.retention import EraseRefused, erase_client
from ariadne.stats import MAX_DAYS

router = APIRouter(prefix="/principals", tags=["principals"])

# Serializa as mudanças que podem tirar o último administrador (despromover, revogar): sem isto
# dois pedidos em simultâneo veriam ambos «ainda há outro» e ficava zero. Um lock consultivo de
# transação, libertado no commit ou rollback.
ADMIN_GUARD_LOCK = 0x41524941444E45  # "ARIADNE"


MAX_QUOTA = 2_000_000_000  # cabe num INTEGER do Postgres
MAX_USAGE_IDS = 200


class PrincipalView(BaseModel):
    id: int
    kind: str
    name: str
    role: str
    scope: str = "full"  # 'mcp': cliente externo, só o MCP e só o conteúdo aberto
    created_at: datetime
    revoked_at: datetime | None
    active: bool  # tem uma chave válida (suspensa ou não)
    suspended: bool = False  # a chave existe mas responde 403 até ser retomada
    suspended_at: datetime | None = None
    external_id: str | None = None  # o id do cliente no portal (criação idempotente)
    monthly_query_quota: int | None = None  # None: sem quota


class CreatedPrincipal(PrincipalView):
    api_key: str | None  # só na criação; num repetido idempotente é null (não volta a mostrar-se)
    idempotent_replay: bool = False  # true: o principal já existia, não se criou outro


ExternalId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="Caller's own id for this principal (unique). Repeating the request with the "
        "same value returns the existing principal instead of creating another.",
    ),
]
Quota = Annotated[
    int,
    Field(ge=1, le=MAX_QUOTA, description="Tool calls per calendar month (UTC), 'mcp' keys only."),
]


class NewPrincipal(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=80, description="Readable name.")]
    kind: Literal["user", "agent"] = "agent"
    role: Literal["admin", "member"] = "member"
    # 'mcp' para as chaves dos clientes externos (versão alojada): só um agente 'member'.
    scope: Literal["full", "mcp"] = "full"
    external_id: ExternalId | None = None
    monthly_query_quota: Quota | None = None


class ErasedClient(BaseModel):
    principal_id: int
    erased: bool  # false: já não existia (o pedido é idempotente)
    tool_calls: int = 0
    queries: int = 0
    query_results: int = 0


class PrincipalPatch(BaseModel):
    """Only the fields sent change. `monthly_query_quota: null` removes the quota."""

    role: Literal["admin", "member"] | None = None
    monthly_query_quota: Quota | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> "PrincipalPatch":
        if not self.model_fields_set & {"role", "monthly_query_quota"}:
            raise ValueError("Send 'role' and/or 'monthly_query_quota'.")
        if "role" in self.model_fields_set and self.role is None:
            raise ValueError("'role' cannot be null.")
        return self


def _view(principal: Principal) -> PrincipalView:
    return PrincipalView(
        id=principal.id,
        kind=principal.kind,
        name=principal.name,
        role=principal.role,
        scope=principal.scope,
        created_at=principal.created_at,
        revoked_at=principal.revoked_at,
        active=principal.api_key_hash is not None,
        suspended=principal.suspended_at is not None,
        suspended_at=principal.suspended_at,
        external_id=principal.external_id,
        monthly_query_quota=principal.monthly_query_quota,
    )


def _lock_admins(session: SessionDep) -> None:
    session.execute(select(func.pg_advisory_xact_lock(ADMIN_GUARD_LOCK)))


def _get(session: SessionDep, principal_id: int) -> Principal:
    """Com o lock dos administradores já tomado: lê o principal depois dele (não antes)."""
    _lock_admins(session)
    found = session.get(Principal, principal_id, populate_existing=True)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Principal {principal_id} does not exist.")
    return found


def _other_active_admins(session: SessionDep, excluding: int) -> int:
    return int(
        session.scalar(
            select(func.count()).where(
                Principal.role == "admin",
                Principal.api_key_hash.is_not(None),
                Principal.id != excluding,
            )
        )
        or 0
    )


@router.get("")
def list_principals(
    session: SessionDep,
    admin: AdminDep,
    external_id: Annotated[
        str | None, Query(max_length=200, description="Only the principal with this external_id.")
    ] = None,
) -> list[PrincipalView]:
    """All principals (revoked ones too), oldest first."""
    stmt = select(Principal).order_by(Principal.id)
    if external_id is not None:
        stmt = stmt.where(Principal.external_id == external_id)
    return [_view(p) for p in session.scalars(stmt)]


@router.get("/usage")
def usage_of_many(
    session: SessionDep,
    admin: AdminDep,
    ids: Annotated[
        str | None,
        Query(
            description="Comma-separated principal ids (up to 200). Omitted: every 'mcp' "
            "principal. Ids that do not exist are left out of the answer.",
            examples=["12,15,18"],
        ),
    ] = None,
    days: Annotated[
        int, Query(ge=1, le=MAX_DAYS, description="Days to count, up to today (UTC).")
    ] = (DEFAULT_USAGE_DAYS),
) -> list[ClientUsage]:
    """Usage per principal, filtered on the server: calls per day (with zeros) and per tool,
    searches that returned nothing, the last use and this month's count against the quota."""
    if ids is None:
        found = list(
            session.scalars(
                select(Principal).where(Principal.scope == "mcp").order_by(Principal.id)
            )
        )
    else:
        wanted = _parse_ids(ids)
        found = list(
            session.scalars(
                select(Principal).where(Principal.id.in_(wanted)).order_by(Principal.id)
            )
        )
    return usage(session, found, days)


@router.get("/{principal_id}/usage")
def usage_of_one(
    session: SessionDep,
    admin: AdminDep,
    principal_id: int,
    days: Annotated[
        int, Query(ge=1, le=MAX_DAYS, description="Days to count, up to today (UTC).")
    ] = (DEFAULT_USAGE_DAYS),
) -> ClientUsage:
    """The same as `GET /api/principals/usage` for one principal (404 if it does not exist)."""
    found = session.get(Principal, principal_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Principal {principal_id} does not exist.")
    return usage(session, [found], days)[0]


def _parse_ids(raw: str) -> list[int]:
    try:
        ids = sorted({int(part) for part in raw.split(",") if part.strip()})
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "'ids' must be comma-separated integers."
        ) from None
    if not ids or len(ids) > MAX_USAGE_IDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Send between 1 and {MAX_USAGE_IDS} ids."
        )
    return ids


def _idempotency_key(body: NewPrincipal, header: str | None) -> str | None:
    """O `external_id` do corpo ou o cabeçalho `Idempotency-Key` (o mesmo valor, escrito de uma
    das duas maneiras); se vierem os dois têm de coincidir."""
    header = header.strip() if header else None
    if header is not None and not 1 <= len(header) <= 200:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Idempotency-Key must be 1 to 200 characters."
        )
    if header and body.external_id and header != body.external_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key and external_id are the same identifier: send one, or equal values.",
        )
    return body.external_id or header or None


def _replay(session: SessionDep, key: str, body: NewPrincipal) -> CreatedPrincipal | None:
    """O principal que já foi criado com este identificador, se existir; 409 se o pedido que o
    criou era outro (um identificador não se reutiliza para outro cliente)."""
    existing = session.scalar(select(Principal).where(Principal.external_id == key))
    if existing is None:
        return None
    if (existing.kind, existing.role, existing.scope, existing.name) != (
        body.kind,
        body.role,
        body.scope,
        body.name.strip(),
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This idempotency key / external_id was already used for a different principal.",
        )
    return CreatedPrincipal(**_view(existing).model_dump(), api_key=None, idempotent_replay=True)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(
    session: SessionDep,
    admin: AdminDep,
    body: NewPrincipal,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(
            description="Same as `external_id` in the body: repeating the request returns the "
            "existing principal (200, `api_key: null`) instead of creating a second one."
        ),
    ] = None,
) -> CreatedPrincipal:
    """Create a principal and return the API key **once only**. Only a `user` can be an
    admin. With an `Idempotency-Key` header (or `external_id`), a repeated request answers 200
    with the existing principal and `api_key: null`: the secret is never shown again."""
    if body.role == "admin" and body.kind != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only a 'user' principal can be an admin.")
    if body.scope == "mcp" and (body.kind != "agent" or body.role != "member"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "An 'mcp' key can only belong to a 'member' agent."
        )
    if body.monthly_query_quota is not None and body.scope != "mcp":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Only an 'mcp' key can have a monthly quota."
        )
    key = _idempotency_key(body, idempotency_key)
    if key is not None:
        replay = _replay(session, key, body)
        if replay is not None:
            response.status_code = status.HTTP_200_OK
            return replay
    admin_id = admin.id
    try:
        created, secret = create_principal(
            session,
            PrincipalKind(body.kind),
            body.name.strip(),
            Role(body.role),
            Scope(body.scope),
            external_id=key,
            monthly_query_quota=body.monthly_query_quota,
        )
    except IntegrityError:
        # Dois pedidos iguais ao mesmo tempo: o outro ganhou a corrida, devolve-se o dele.
        session.rollback()
        replay = _replay(session, key, body) if key is not None else None
        if replay is None:
            raise
        response.status_code = status.HTTP_200_OK
        return replay
    view = _view(created)
    session.commit()
    with tracked_call(
        session,
        principal_id=admin_id,
        session_id=None,
        tool="principal_create",
        arguments=body.model_dump() | {"external_id": key},
    ) as call:
        call.result = {"principal_id": view.id}
    return CreatedPrincipal(**view.model_dump(), api_key=secret)


@router.patch("/{principal_id}")
def change_principal(
    session: SessionDep, admin: AdminDep, principal_id: int, body: PrincipalPatch
) -> PrincipalView:
    """Change the role and/or the monthly quota (only the fields sent change; `null` removes the
    quota, which only an 'mcp' key can have). You cannot demote yourself if you are the last
    admin, nor make an agent an admin."""
    target = _get(session, principal_id)
    if "monthly_query_quota" in body.model_fields_set:
        if body.monthly_query_quota is not None and target.scope != "mcp":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Only an 'mcp' key can have a monthly quota."
            )
        target.monthly_query_quota = body.monthly_query_quota
    if body.role is not None:
        if body.role == "admin" and target.kind != "user":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Only a 'user' principal can be an admin."
            )
        if (
            body.role != "admin"
            and target.role == "admin"
            and target.api_key_hash is not None
            and not _other_active_admins(session, target.id)
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "At least one admin with an active key must remain."
            )
        target.role = body.role
    view = _view(target)
    session.commit()
    return view


def _suspension_target(session: SessionDep, principal_id: int) -> Principal:
    target = _get(session, principal_id)
    if target.scope != "mcp":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only external client keys (scope 'mcp') can be suspended; the owner's keys cannot.",
        )
    if target.api_key_hash is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The key was revoked: it cannot be suspended or resumed."
        )
    return target


def _set_suspended(
    session: SessionDep, admin_id: int, principal_id: int, suspended: bool
) -> PrincipalView:
    target = _suspension_target(session, principal_id)
    changed = (target.suspended_at is not None) != suspended
    if changed:
        target.suspended_at = func.now() if suspended else None
        session.flush()
        session.refresh(target)
    view = _view(target)
    session.commit()
    with tracked_call(
        session,
        principal_id=admin_id,
        session_id=None,
        tool="principal_suspend" if suspended else "principal_resume",
        arguments={"principal_id": principal_id},
    ) as call:
        call.result = {"principal_id": principal_id, "changed": changed}
    return view


@router.post("/{principal_id}/suspend")
def suspend(session: SessionDep, admin: AdminDep, principal_id: int) -> PrincipalView:
    """Suspend an external client key ('mcp'): it answers 403 at once but keeps its value and its
    history, so `resume` brings the same key back. Idempotent. A revoked key answers 409; so does
    any key that is not 'mcp'."""
    return _set_suspended(session, admin.id, principal_id, True)


@router.post("/{principal_id}/resume")
def resume(session: SessionDep, admin: AdminDep, principal_id: int) -> PrincipalView:
    """Lift a suspension: the same key works again, with nothing to reconfigure. Idempotent (a key
    that is not suspended is returned as it is)."""
    return _set_suspended(session, admin.id, principal_id, False)


@router.delete("/{principal_id}")
def revoke(
    session: SessionDep, admin: AdminDep, principal_id: int, response: Response
) -> PrincipalView:
    """Revoke the principal's API key (it stops working immediately). The last admin key cannot
    be revoked."""
    target = _get(session, principal_id)
    if (
        target.role == "admin"
        and target.api_key_hash is not None
        and not _other_active_admins(session, target.id)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "At least one admin with an active key must remain."
        )
    if target.api_key_hash is not None:
        target.api_key_hash = None
        target.revoked_at = func.now()
        session.flush()
        session.refresh(target)
    view = _view(target)
    session.commit()
    return view


@router.post("/{principal_id}/erase")
def erase(session: SessionDep, admin: AdminDep, principal_id: int) -> ErasedClient:
    """Apaga de vez um cliente externo (chave `mcp`) e todo o histórico dele: chamadas, pesquisas,
    resultados e feedback, e o nome no registo da criação (RGPD; o «apagar conta» do portal).
    Idempotente: um principal que já não existe responde `erased: false`. Os principais `full` (os
    do dono, administradores incluídos) nunca se apagam: 409."""
    admin_id = admin.id
    try:
        counts = erase_client(session, principal_id)
    except EraseRefused:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only external client keys (scope 'mcp') can be erased; the owner's principals cannot.",
        ) from None
    session.commit()
    erased = ErasedClient(principal_id=principal_id, erased=counts is not None, **(counts or {}))
    with tracked_call(
        session,
        principal_id=admin_id,
        session_id=None,
        tool="principal_erase",
        arguments={"principal_id": principal_id},
    ) as call:
        call.result = erased.model_dump()
    return erased
