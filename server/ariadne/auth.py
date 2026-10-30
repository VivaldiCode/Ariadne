"""Chaves de API: identificam o principal em todas as chamadas (secção 14.4)."""

import hashlib
import secrets
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne.db.models import Principal

KEY_PREFIX = "ariadne_"


class PrincipalKind(StrEnum):
    user = "user"
    agent = "agent"


class Role(StrEnum):
    admin = "admin"  # gere principais e chaves
    member = "member"


class Scope(StrEnum):
    full = "full"  # a API REST e o MCP, com o que o papel deixa
    mcp = "mcp"  # clientes externos: só o MCP e só o conteúdo da classe aberto


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    # A chave tem 256 bits aleatórios: SHA-256 chega, não precisa de um hash lento.
    return hashlib.sha256(key.encode()).hexdigest()


def create_principal(
    session: Session,
    kind: PrincipalKind,
    name: str,
    role: Role = Role.member,
    scope: Scope = Scope.full,
    external_id: str | None = None,
    monthly_query_quota: int | None = None,
) -> tuple[Principal, str]:
    """Cria o principal e devolve a chave em claro, que não volta a ser recuperável. Uma chave
    `mcp` só pode ser de um agente `member` (o Postgres também o garante)."""
    if scope is Scope.mcp and (kind is not PrincipalKind.agent or role is not Role.member):
        raise ValueError("An 'mcp' key can only belong to a 'member' agent.")
    if monthly_query_quota is not None and scope is not Scope.mcp:
        raise ValueError("Only an 'mcp' key can have a monthly quota.")
    key = generate_api_key()
    principal = Principal(
        kind=kind.value,
        name=name,
        role=role.value,
        scope=scope.value,
        external_id=external_id,
        monthly_query_quota=monthly_query_quota,
        api_key_hash=hash_api_key(key),
    )
    session.add(principal)
    session.flush()
    return principal, key


def find_principal(session: Session, key: str) -> Principal | None:
    stmt = select(Principal).where(Principal.api_key_hash == hash_api_key(key))
    return session.scalars(stmt).one_or_none()
