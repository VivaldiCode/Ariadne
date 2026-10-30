"""A configuração efetiva das integrações: o cofre ganha à variável de ambiente.

`IntegrationConfig` junta as `Settings` do ambiente (a base) com o que está no cofre e entrega uma
`Settings` efetiva. Relê o cofre no máximo de `VAULT_CACHE_SECONDS` em `VAULT_CACHE_SECONDS`
segundos (e logo que este processo o altera), por isso o servidor e o worker veem uma mudança sem
reiniciar. Sem chave mestra, ou com a errada, vale só o ambiente e o motivo fica em `problem`.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ariadne.config import Settings
from ariadne.vault.crypto import VaultError
from ariadne.vault.registry import FIELDS
from ariadne.vault.store import DbVault

logger = logging.getLogger(__name__)

Source = Literal["vault", "env", "default", "none"]


@dataclass(frozen=True)
class Resolved:
    value: str
    source: Source


def coerce(key: str, raw: str) -> Any:
    """O valor do cofre (texto) no tipo do campo de `Settings`; `ValueError` se não serve."""
    annotation: Any = Settings.model_fields[key].annotation
    try:
        return TypeAdapter(annotation).validate_python(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid value for {key.upper()}: {exc.errors()[0]['msg']}") from exc


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class IntegrationConfig:
    def __init__(
        self,
        base: Settings,
        sessions: sessionmaker[Session] | None,
        *,
        ttl: float | None = None,
    ) -> None:
        self.base = base
        self.vault = DbVault(sessions, base.ariadne_master_key) if sessions is not None else None
        self.ttl = base.vault_cache_seconds if ttl is None else ttl
        self.problem: str | None = None  # porque o cofre não está a valer (chave em falta, errada)
        self._effective = base
        self._values: dict[str, str] = {}
        self._fingerprint: tuple[int, datetime | None] | None = None
        self._checked = float("-inf")
        self._lock = threading.Lock()

    # --- leitura ---

    @property
    def values(self) -> dict[str, str]:
        """Os valores do cofre que estão a valer (copiados)."""
        self.effective()
        return dict(self._values)

    def effective(self) -> Settings:
        """As `Settings` com o cofre por cima. O mesmo objeto até o cofre mudar."""
        if time.monotonic() - self._checked >= self.ttl:
            self.refresh()
        return self._effective

    def resolve(self, key: str) -> Resolved:
        """O valor que vale para `key` e de onde vem (cofre > ambiente > omissão)."""
        self.effective()
        if key in self._values:
            return Resolved(self._values[key], "vault")
        value = _text(getattr(self.base, key))
        if not value:
            return Resolved("", "none")
        default = Settings.model_fields[key].default
        return Resolved(value, "default" if value == _text(default) else "env")

    def invalidate(self) -> None:
        """Este processo mudou o cofre: a próxima leitura volta a ele."""
        self._checked = float("-inf")

    def refresh(self) -> bool:
        """Volta a ler o cofre se mudou. Devolve se as `Settings` efetivas mudaram."""
        if self.vault is None:
            self._checked = time.monotonic()
            return False
        with self._lock:
            self._checked = time.monotonic()
            before = self._effective
            try:
                with self.vault.sessions() as session:
                    fingerprint = self.vault.fingerprint(session)
                    if fingerprint == self._fingerprint and self.problem is None:
                        return False
                    if fingerprint[0] and not self.vault.has_key:
                        self._apply(
                            {},
                            fingerprint,
                            "the vault has values but ARIADNE_MASTER_KEY is missing",
                        )
                    else:
                        self._apply(self.vault.load_all(session), fingerprint, None)
            except VaultError as exc:
                self._apply({}, None, str(exc))
            except SQLAlchemyError as exc:
                # Sem a tabela (migração por aplicar) ou sem base de dados: fica o que havia.
                if self.problem is None:
                    logger.warning("Vault unreadable (%s); using the last configuration", exc)
                return False
            return self._effective is not before

    def _apply(
        self,
        values: dict[str, str],
        fingerprint: tuple[int, datetime | None] | None,
        problem: str | None,
    ) -> None:
        if problem and problem != self.problem:
            logger.error("Vault disabled, using only the environment variables: %s", problem)
        self.problem = problem
        self._fingerprint = fingerprint
        overrides: dict[str, Any] = {}
        accepted: dict[str, str] = {}
        for key, raw in values.items():
            if key not in FIELDS:
                continue  # uma definição que já não existe
            try:
                overrides[key] = coerce(key, raw)
            except ValueError as exc:
                logger.warning("Ignored in the vault: %s", exc)
                continue
            accepted[key] = raw
        if accepted == self._values and self._effective is not self.base:
            return
        self._values = accepted
        self._effective = self.base.model_copy(update=overrides) if overrides else self.base


def effective_settings() -> Settings:
    """As `Settings` do ambiente com o cofre por cima, para os comandos da CLI que não abrem os
    `Services`. Se a base de dados não responde, devolve só o ambiente."""
    from ariadne.config import get_settings
    from ariadne.db.session import make_engine

    base = get_settings()
    try:
        engine = make_engine(base.database_url)
    except Exception:
        return base
    try:
        return IntegrationConfig(base, sessionmaker(engine)).effective()
    finally:
        engine.dispose()
