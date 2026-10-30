"""O registo de backups e releases (`backup_run`): o que a app mostra em «Operação»."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne.config import Settings
from ariadne.db.models import BackupRun

FAILED_RETRY = timedelta(hours=6)


def start(session: Session, *, kind: str, name: str, requested_by: str | None = None) -> BackupRun:
    row = BackupRun(kind=kind, status="running", name=name, requested_by=requested_by)
    session.add(row)
    session.flush()
    return row


def reap_stale(session: Session, hours: float, now: datetime | None = None) -> int:
    """Um backup ou release «a correr» há mais de `hours` horas (o worker morreu, o contentor foi
    reiniciado) não está a correr: passa a `failed`, para a app e o agendamento não ficarem presos
    nem a esperar para sempre. Sem commit."""
    limit = (now or datetime.now(UTC)) - timedelta(hours=hours)
    rows = session.scalars(
        select(BackupRun).where(BackupRun.status == "running", BackupRun.started_at < limit)
    ).all()
    for row in rows:
        row.status = "failed"
        row.finished_at = datetime.now(UTC)
        row.error = f"no activity for more than {hours:g} h (the worker probably died midway)"
    return len(rows)


def latest(session: Session, kind: str, *, ok_only: bool = False) -> BackupRun | None:
    query = select(BackupRun).where(BackupRun.kind == kind)
    if ok_only:
        query = query.where(BackupRun.status == "ok")
    return session.scalars(query.order_by(BackupRun.started_at.desc(), BackupRun.id.desc())).first()


def history(session: Session, limit: int = 10) -> list[BackupRun]:
    return list(
        session.scalars(
            select(BackupRun)
            .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
            .limit(limit)
        )
    )


@dataclass
class Schedule:
    enabled: bool
    interval_days: float
    last_started: datetime | None
    next_due: datetime | None
    due_now: bool
    reason: str


def schedule(session: Session, settings: Settings, now: datetime | None = None) -> Schedule:
    """Quando é a próxima release semanal. Só corre com `RELEASE_ENABLED=true` e a chave pública
    configurada; uma que falhou repete-se passadas umas horas, não de minuto a minuto."""
    now = now or datetime.now(UTC)
    interval = timedelta(days=settings.release_interval_days)
    last_good = latest(session, "release", ok_only=True)
    last_any = latest(session, "release")
    running = last_any is not None and last_any.status == "running"
    base = last_good.started_at if last_good else None
    next_due = (base + interval) if base else now
    if not settings.release_enabled:
        return Schedule(False, settings.release_interval_days, base, None, False, "off")
    if not settings.release_age_recipient.strip():
        return Schedule(
            True, settings.release_interval_days, base, None, False, "no RELEASE_AGE_RECIPIENT"
        )
    if running:
        return Schedule(True, settings.release_interval_days, base, next_due, False, "running")
    if last_any is not None and last_any.status == "failed":
        retry_at = last_any.started_at + FAILED_RETRY
        if now < retry_at:
            return Schedule(True, settings.release_interval_days, base, retry_at, False, "failed")
    return Schedule(True, settings.release_interval_days, base, next_due, now >= next_due, "ok")


def finish(
    session: Session,
    row: BackupRun,
    *,
    status: str,
    error: str | None = None,
    **fields: Any,
) -> None:
    row.status = status
    row.finished_at = datetime.now(UTC)
    row.error = error[:2000] if error else None
    for key, value in fields.items():
        setattr(row, key, value)
