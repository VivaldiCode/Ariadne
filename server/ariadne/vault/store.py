"""O cofre na base de dados: valores cifrados, a verificação da chave mestra e a auditoria.

`SecretStore` é a interface de leitura (um valor por nome); `DbVault` guarda e decifra; o
`IntegrationConfig` (`config.py`) junta-o às variáveis de ambiente. Nada aqui devolve ou regista
um valor fora de `get`/`load_all`, e a auditoria nunca leva o valor.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.db.models import IntegrationAudit, IntegrationSetting
from ariadne.vault import crypto
from ariadne.vault.crypto import MasterKeyMissing, MasterKeyWrong, VaultError
from ariadne.vault.registry import FIELDS, OWNER

logger = logging.getLogger(__name__)

CHECK_ROW = "_check"  # o valor conhecido que prova qual é a chave mestra
CHECK_TEXT = "ariadne-vault-v1"


class SecretStore(Protocol):
    """Uma fonte de valores por nome (`calibre_api_key`). `None` = não tem."""

    def get(self, key: str) -> str | None: ...


@dataclass(frozen=True)
class EntryInfo:
    key: str
    integration: str
    is_secret: bool
    updated_at: datetime
    updated_by: str | None


@dataclass(frozen=True)
class AuditEntry:
    id: int
    at: datetime
    actor: str
    action: str
    key: str | None
    integration: str | None
    detail: str | None


def _real(key: str) -> bool:
    return not key.startswith("_")


class DbVault:
    """Os valores cifrados da tabela `integration_setting`."""

    def __init__(self, sessions: sessionmaker[Session], master_key: str) -> None:
        self.sessions = sessions
        self._raw_key = master_key

    # --- chave mestra ---

    @property
    def has_key(self) -> bool:
        return bool(self._raw_key.strip())

    def _key(self) -> bytes:
        return crypto.parse_master_key(self._raw_key)  # MasterKeyMissing/MasterKeyInvalid

    def _verify(self, session: Session, key: bytes) -> None:
        """A chave mestra tem de ser a que cifrou o cofre. Num cofre vazio, fixa-a."""
        check = session.get(IntegrationSetting, CHECK_ROW)
        if check is not None:
            crypto.decrypt(key, CHECK_ROW, check.ciphertext)  # MasterKeyWrong
            return
        other = session.scalars(
            select(IntegrationSetting).where(IntegrationSetting.key != CHECK_ROW).limit(1)
        ).first()
        if other is not None:
            crypto.decrypt(key, other.key, other.ciphertext)  # MasterKeyWrong
        session.add(
            IntegrationSetting(
                key=CHECK_ROW,
                integration="_vault",
                ciphertext=crypto.encrypt(key, CHECK_ROW, CHECK_TEXT),
                is_secret=False,
            )
        )
        session.flush()

    # --- leitura ---

    def fingerprint(self, session: Session) -> tuple[int, datetime | None]:
        """Muda sempre que o cofre muda (nº de linhas e última alteração)."""
        row = session.execute(
            select(func.count(), func.max(IntegrationSetting.updated_at)).where(
                IntegrationSetting.key != CHECK_ROW
            )
        ).one()
        return int(row[0]), row[1]

    def load_all(self, session: Session) -> dict[str, str]:
        """Todos os valores decifrados. `VaultError` se falta ou está errada a chave mestra."""
        rows = session.scalars(select(IntegrationSetting).order_by(IntegrationSetting.key)).all()
        if not any(_real(r.key) for r in rows):
            return {}
        key = self._key()
        check = next((r for r in rows if r.key == CHECK_ROW), None)
        if check is not None:
            crypto.decrypt(key, CHECK_ROW, check.ciphertext)
        return {r.key: crypto.decrypt(key, r.key, r.ciphertext) for r in rows if _real(r.key)}

    def get(self, key: str) -> str | None:
        with self.sessions() as session:
            row = session.get(IntegrationSetting, key)
            if row is None or not _real(key):
                return None
            return crypto.decrypt(self._key(), key, row.ciphertext)

    def entries(self, session: Session) -> dict[str, EntryInfo]:
        rows = session.scalars(select(IntegrationSetting)).all()
        return {
            r.key: EntryInfo(r.key, r.integration, r.is_secret, r.updated_at, r.updated_by)
            for r in rows
            if _real(r.key)
        }

    # --- escrita (cada uma faz commit e audita na mesma transação) ---

    def put(self, key: str, value: str, actor: str, action: str = "set", detail: str = "") -> None:
        field = FIELDS[key]
        master = self._key()
        with self.sessions() as session, session.begin():
            self._verify(session, master)
            now = datetime.now(UTC)
            blob = crypto.encrypt(master, key, value)
            row = session.get(IntegrationSetting, key)
            if row is None:
                session.add(
                    IntegrationSetting(
                        key=key,
                        integration=OWNER[key],
                        ciphertext=blob,
                        is_secret=field.secret,
                        created_at=now,
                        updated_at=now,
                        updated_by=actor,
                    )
                )
            else:
                row.ciphertext, row.updated_at, row.updated_by = blob, now, actor
            self.audit(session, actor, action, key, detail)

    def remove(self, key: str, actor: str) -> bool:
        with self.sessions() as session, session.begin():
            gone = session.scalars(
                delete(IntegrationSetting)
                .where(IntegrationSetting.key == key)
                .returning(IntegrationSetting.key)
            ).first()
            if gone is not None:
                self.audit(session, actor, "delete", key, "")
            return gone is not None

    def rekey(self, new_master_key: str, actor: str) -> int:
        """Re-cifra tudo com outra chave mestra, numa só transação. Devolve quantos valores."""
        old = self._key()
        new = crypto.parse_master_key(new_master_key)
        with self.sessions() as session, session.begin():
            rows = session.scalars(select(IntegrationSetting)).all()
            plain = {r.key: crypto.decrypt(old, r.key, r.ciphertext) for r in rows}
            now = datetime.now(UTC)
            for r in rows:
                r.ciphertext = crypto.encrypt(new, r.key, plain[r.key])
                if _real(r.key):
                    r.updated_at, r.updated_by = now, actor
            count = sum(1 for k in plain if _real(k))
            self.audit(session, actor, "rekey", None, f"{count} values re-encrypted")
        self._raw_key = new_master_key
        return count

    def wipe(self, actor: str) -> int:
        """Apaga todo o cofre (a chave mestra perdeu-se): os valores não se recuperam e voltam a
        valer as variáveis de ambiente. A auditoria fica. Devolve quantos valores tinha."""
        with self.sessions() as session, session.begin():
            keys = session.scalars(
                delete(IntegrationSetting).returning(IntegrationSetting.key)
            ).all()
            count = sum(1 for k in keys if _real(k))
            self.audit(session, actor, "reset", None, f"{count} values deleted")
        return count

    # --- auditoria ---

    def audit(
        self, session: Session, actor: str, action: str, key: str | None, detail: str = ""
    ) -> None:
        session.add(
            IntegrationAudit(
                actor=actor,
                action=action,
                key=key,
                integration=OWNER.get(key) if key else None,
                detail=detail or None,
            )
        )

    def record(self, actor: str, action: str, key: str | None, detail: str = "") -> None:
        """Uma linha de auditoria avulsa (ex.: um teste de ligação)."""
        with self.sessions() as session, session.begin():
            self.audit(session, actor, action, key, detail)

    def audit_log(self, limit: int = 100) -> list[AuditEntry]:
        with self.sessions() as session:
            rows = session.scalars(
                select(IntegrationAudit).order_by(IntegrationAudit.id.desc()).limit(limit)
            ).all()
            return [
                AuditEntry(r.id, r.at, r.actor, r.action, r.key, r.integration, r.detail)
                for r in rows
            ]


__all__ = [
    "AuditEntry",
    "DbVault",
    "EntryInfo",
    "MasterKeyMissing",
    "MasterKeyWrong",
    "SecretStore",
    "VaultError",
]
