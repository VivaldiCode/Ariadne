"""As operações de administração do cofre (API do dashboard e CLI): ver, definir, rodar, gerar,
apagar, importar do ambiente. Nenhuma devolve um valor em claro: um segredo mostra-se como
«definido» e os últimos 4 caracteres; só `generate`/`rotate` devolvem, uma vez, o valor que acabam
de inventar (é o único jeito de o levar para o outro lado, ex. o NAS)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from ariadne.config import Settings
from ariadne.vault import crypto
from ariadne.vault.config import IntegrationConfig, Resolved, coerce
from ariadne.vault.crypto import VaultError
from ariadne.vault.registry import BY_ID, FIELDS, INTEGRATIONS, OWNER, Field
from ariadne.vault.store import DbVault

MAX_VALUE = 4096


class VaultNotEnabled(VaultError):
    """Sem base de dados ou sem chave mestra: não se pode escrever."""


class UnknownField(VaultError):
    pass


class InvalidValue(VaultError):
    pass


@dataclass(frozen=True)
class FieldView:
    key: str
    env: str
    label: str
    kind: str
    secret: bool
    help: str
    generable: bool
    shared_with: str
    defined: bool
    source: str  # vault | env | default | none
    hint: str  # segredos: os últimos 4 caracteres (ou nada)
    value: str | None  # só para o que não é segredo
    updated_at: datetime | None
    updated_by: str | None


@dataclass(frozen=True)
class IntegrationView:
    id: str
    label: str
    description: str
    configured: bool  # todos os campos obrigatórios têm valor
    fields: list[FieldView]


@dataclass(frozen=True)
class VaultState:
    enabled: bool  # dá para escrever (há chave mestra válida e certa)
    has_master_key: bool
    entries: int
    problem: str | None


def _field(key: str) -> Field:
    try:
        return FIELDS[key]
    except KeyError as exc:
        raise UnknownField(f"Unknown field: {key!r}.") from exc


def validate(key: str, value: str) -> str:
    """O valor limpo, ou `InvalidValue`. Nunca repete o valor na mensagem."""
    field = _field(key)
    value = value.strip()
    if not value:
        raise InvalidValue("The value is empty (to go back to the environment, delete the entry).")
    if len(value) > MAX_VALUE:
        raise InvalidValue(f"The value is too long (maximum {MAX_VALUE} characters).")
    if any(ord(c) < 32 for c in value):
        raise InvalidValue("The value has line breaks or control characters.")
    if field.kind == "url":
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise InvalidValue("The URL must start with http:// or https://.")
    try:
        coerce(key, value)
    except ValueError as exc:
        raise InvalidValue(str(exc).replace(value, "…")) from exc
    return value


class VaultAdmin:
    def __init__(self, config: IntegrationConfig, base: Settings | None = None) -> None:
        self.config = config
        self.base = base or config.base

    @property
    def vault(self) -> DbVault:
        if self.config.vault is None:
            raise VaultNotEnabled("The vault needs the database.")
        return self.config.vault

    def _writable(self) -> DbVault:
        vault = self.vault
        crypto.parse_master_key(self.base.ariadne_master_key)  # MasterKeyMissing/Invalid
        return vault

    # --- leitura ---

    def state(self) -> VaultState:
        self.config.refresh()
        entries = len(self.config.values)
        return VaultState(
            enabled=self.config.vault is not None
            and self.config.problem is None
            and self._has_valid_key(),
            has_master_key=bool(self.base.ariadne_master_key.strip()),
            entries=entries,
            problem=self.config.problem or self._key_problem(),
        )

    def _has_valid_key(self) -> bool:
        return self._key_problem() is None

    def _key_problem(self) -> str | None:
        try:
            crypto.parse_master_key(self.base.ariadne_master_key)
        except VaultError as exc:
            return str(exc)
        return None

    def view(self) -> list[IntegrationView]:
        self.config.refresh()
        meta = {}
        if self.config.vault is not None:
            try:
                with self.config.vault.sessions() as session:
                    meta = self.config.vault.entries(session)
            except Exception:  # sem tabela: mostra só o ambiente
                meta = {}
        views: list[IntegrationView] = []
        for integration in INTEGRATIONS:
            fields = [
                self._field_view(f, self.config.resolve(f.key), meta) for f in integration.fields
            ]
            views.append(
                IntegrationView(
                    integration.id,
                    integration.label,
                    integration.description,
                    configured=_configured(integration.id, fields),
                    fields=fields,
                )
            )
        return views

    def _field_view(self, f: Field, resolved: Resolved, meta: dict[str, Any]) -> FieldView:
        entry = meta.get(f.key)
        return FieldView(
            key=f.key,
            env=f.env,
            label=f.label,
            kind=f.kind,
            secret=f.secret,
            help=f.help,
            generable=f.generable,
            shared_with=f.shared_with,
            defined=bool(resolved.value),
            source=resolved.source,
            hint=crypto.hint(resolved.value) if f.secret else "",
            value=None if f.secret else (resolved.value or ""),
            updated_at=entry.updated_at if entry else None,
            updated_by=entry.updated_by if entry else None,
        )

    # --- escrita ---

    def set_value(self, key: str, value: str, actor: str, action: str = "set") -> None:
        clean = validate(key, value)
        self._writable().put(key, clean, actor, action)
        self.config.invalidate()

    def generate(self, key: str, actor: str, action: str = "generate") -> str:
        """Inventa um valor para um segredo nosso e devolve-o, esta única vez."""
        if not _field(key).generable:
            raise InvalidValue(
                "Ariadne does not generate this secret: it comes from the originating service."
            )
        value = crypto.random_secret()
        self._writable().put(key, value, actor, action)
        self.config.invalidate()
        return value

    def delete(self, key: str, actor: str) -> bool:
        _field(key)
        removed = self._writable().remove(key, actor)
        self.config.invalidate()
        return removed

    def import_env(
        self, actor: str, *, overwrite: bool = False, dry_run: bool = False
    ) -> dict[str, list[str]]:
        """Copia para o cofre o que está nas variáveis de ambiente (`base` = as Settings do
        ambiente). Só nomes: devolve o que copiou, o que já estava no cofre e o que
        não tinha valor."""
        vault = None if dry_run else self._writable()
        existing = set(self.config.values) if self.config.vault is not None else set()
        report: dict[str, list[str]] = {"imported": [], "kept": [], "empty": []}
        for key, field in FIELDS.items():
            raw = getattr(self.base, key)
            value = "" if raw is None else str(raw)
            default = Settings.model_fields[key].default
            if not value or value == ("" if default is None else str(default)):
                report["empty"].append(field.env)
                continue
            if key in existing and not overwrite:
                report["kept"].append(field.env)
                continue
            try:
                clean = validate(key, value)
            except InvalidValue as exc:
                report["empty"].append(f"{field.env} ({exc})")
                continue
            if vault is not None:
                vault.put(key, clean, actor, "import", "from an environment variable")
            report["imported"].append(field.env)
        self.config.invalidate()
        return report


def _configured(integration_id: str, fields: list[FieldView]) -> bool:
    """Tem o mínimo para funcionar (os campos obrigatórios); sem obrigatórios, qualquer valor."""
    if integration_id == "kaggle":
        have = {f.key for f in fields if f.defined}
        return "kaggle_username" in have and bool(have & {"kaggle_api_token", "kaggle_key"})
    required = [f for f in fields if FIELDS[f.key].required]
    if required:
        return all(f.defined for f in required)
    return any(f.defined and f.source != "default" for f in fields)


__all__ = [
    "BY_ID",
    "OWNER",
    "InvalidValue",
    "UnknownField",
    "VaultAdmin",
    "VaultNotEnabled",
]
