"""Integrações e segredos na API REST (STRUCTURE.md, secção 11): só administradores.

O cofre (`ariadne.vault`) guarda cifrados os segredos e as opções das integrações. Esta API nunca
devolve um segredo em claro: mostra «definido», a origem (cofre ou ambiente) e os últimos 4
caracteres. A única exceção é `generate`/`rotate` de um segredo que o Ariadne inventa (a chave do
Calibre, a do Jobe): devolvem o valor novo uma vez, para o levar ao outro lado (NAS).
"""

import contextlib
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ariadne.api.deps import AdminDep, ServicesDep
from ariadne.services import Services
from ariadne.vault.admin import (
    IntegrationView,
    InvalidValue,
    UnknownField,
    VaultAdmin,
    VaultState,
)
from ariadne.vault.crypto import VaultError
from ariadne.vault.registry import BY_ID
from ariadne.vault.testers import run_test

router = APIRouter(prefix="/integrations", tags=["integrations"])


class IntegrationsOut(BaseModel):
    vault: VaultState
    integrations: list[IntegrationView]


class ValueIn(BaseModel):
    value: str = Field(max_length=8192, description="The new value (never returned).")


class RotateIn(BaseModel):
    value: str | None = Field(default=None, max_length=8192)


class GeneratedOut(BaseModel):
    key: str
    value: str  # só nesta resposta
    shown_once: bool = True


class TestOut(BaseModel):
    __test__ = False

    integration: str
    ok: bool
    message: str


class AuditOut(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    key: str | None
    integration: str | None
    detail: str | None


def _admin(services: Services) -> VaultAdmin:
    if services.config is None:
        raise HTTPException(503, "The integrations vault is not available on this server.")
    return VaultAdmin(services.config)


def _fail(exc: VaultError) -> HTTPException:
    if isinstance(exc, UnknownField):
        return HTTPException(404, str(exc))
    if isinstance(exc, InvalidValue):
        return HTTPException(422, str(exc))
    return HTTPException(409, str(exc))  # chave mestra em falta/errada, cofre indisponível


@router.get("")
def list_integrations(_: AdminDep, services: ServicesDep) -> IntegrationsOut:
    """Each integration with its fields: set or not, the origin and the last change."""
    admin = _admin(services)
    return IntegrationsOut(vault=admin.state(), integrations=admin.view())


@router.get("/audit")
def audit_log(
    _: AdminDep,
    services: ServicesDep,
    limit: int = Query(100, ge=1, le=500),
) -> list[AuditOut]:
    """Who changed what and when (without the values)."""
    admin = _admin(services)
    if admin.config.vault is None:
        return []
    return [AuditOut(**vars(e)) for e in admin.vault.audit_log(limit)]


@router.put("/fields/{key}", status_code=204)
def set_field(key: str, body: ValueIn, admin_principal: AdminDep, services: ServicesDep) -> None:
    """Set (or replace) a field value in the vault."""
    try:
        _admin(services).set_value(key, body.value, admin_principal.name)
    except VaultError as exc:
        raise _fail(exc) from exc
    services.sync_config()


@router.delete("/fields/{key}", status_code=204)
def delete_field(key: str, admin_principal: AdminDep, services: ServicesDep) -> None:
    """Remove the value from the vault: the environment variable applies again (if any)."""
    try:
        _admin(services).delete(key, admin_principal.name)
    except VaultError as exc:
        raise _fail(exc) from exc
    services.sync_config()


@router.post("/fields/{key}/generate")
def generate_field(key: str, admin_principal: AdminDep, services: ServicesDep) -> GeneratedOut:
    """Generate a random value for one of our own secrets and return it, this one time only."""
    try:
        value = _admin(services).generate(key, admin_principal.name)
    except VaultError as exc:
        raise _fail(exc) from exc
    services.sync_config()
    return GeneratedOut(key=key, value=value)


@router.post("/fields/{key}/rotate")
def rotate_field(
    key: str, body: RotateIn, admin_principal: AdminDep, services: ServicesDep
) -> GeneratedOut | None:
    """Rotate a secret: with `value` it stores it; without, it generates one (if it is ours)."""
    admin = _admin(services)
    try:
        if body.value is not None:
            admin.set_value(key, body.value, admin_principal.name, action="rotate")
            result = None
        else:
            result = GeneratedOut(
                key=key, value=admin.generate(key, admin_principal.name, "rotate")
            )
    except VaultError as exc:
        raise _fail(exc) from exc
    services.sync_config()
    return result


@router.post("/{integration_id}/test")
def test_integration(
    integration_id: str, admin_principal: AdminDep, services: ServicesDep
) -> TestOut:
    """\"Test connection\": a short request to the service with the current configuration."""
    if integration_id not in BY_ID:
        raise HTTPException(404, f"Unknown integration: {integration_id!r}.")
    admin = _admin(services)
    services.sync_config()
    result = run_test(integration_id, services.settings)
    if admin.config.vault is not None:
        with contextlib.suppress(Exception):  # a auditoria não impede o teste
            admin.vault.record(
                admin_principal.name,
                "test",
                None,
                f"{integration_id}: {'ok' if result.ok else 'failed'}",
            )
    return TestOut(integration=integration_id, ok=result.ok, message=result.message)
