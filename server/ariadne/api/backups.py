"""Backups e releases no dashboard (Fase 12; módulo «Operação»): o último backup local e a última
release (tamanho, partes, publicação, ensaio de restauro) e o próximo agendamento. Qualquer
principal lê; construir uma release e registar um ensaio é só dos administradores."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.db.models import BackupRun, Job
from ariadne.release import runs
from ariadne.release.age import AgeError
from ariadne.release.run import ReleaseConfigError, check_release_config
from ariadne.workers.release import KIND, enqueue_release

router = APIRouter(tags=["backups"])


class RunView(BaseModel):
    id: int
    kind: str
    status: str
    name: str | None
    started_at: datetime
    finished_at: datetime | None
    size_bytes: int | None
    parts: int | None
    requested_by: str | None
    error: str | None
    published_at: datetime | None
    published_url: str | None
    rehearsal_status: str | None
    rehearsed_at: datetime | None
    counts: dict[str, int] | None = None  # o que a release leva (só contagens)


class ScheduleView(BaseModel):
    enabled: bool  # RELEASE_ENABLED
    recipient_configured: bool
    interval_days: float
    next_due: datetime | None
    due_now: bool
    reason: str  # ok | desligado | a correr | falhou | sem RELEASE_AGE_RECIPIENT
    build_pending: bool  # há um job release_build à espera ou a correr


class RehearsalView(BaseModel):
    last_ok_at: datetime | None
    overdue: bool  # nunca ensaiada, ou o último ensaio tem mais de RELEASE_REHEARSAL_DAYS
    max_days: int


class BackupsView(BaseModel):
    schedule: ScheduleView
    rehearsal: RehearsalView
    last_backup: RunView | None
    last_release: RunView | None
    history: list[RunView]


class RehearsalBody(BaseModel):
    status: Literal["ok", "failed"]
    notes: str = Field(default="", max_length=1000)


def view(row: BackupRun, *, detail: bool = True) -> RunView:
    """`detail=False` (agentes) esconde o erro e quem pediu: só pessoas veem esses pormenores."""
    counts = row.details.get("counts") if isinstance(row.details, dict) else None
    return RunView(
        id=row.id,
        kind=row.kind,
        status=row.status,
        name=row.name,
        started_at=row.started_at,
        finished_at=row.finished_at,
        size_bytes=row.size_bytes,
        parts=row.parts,
        requested_by=row.requested_by if detail else None,
        error=row.error if detail else None,
        published_at=row.published_at,
        published_url=row.published_url,
        rehearsal_status=row.rehearsal_status,
        rehearsed_at=row.rehearsed_at,
        counts=counts if isinstance(counts, dict) else None,
    )


@router.get("/backups")
def get_backups(services: ServicesDep, session: SessionDep, principal: PrincipalDep) -> BackupsView:
    """Backup and release status: the latest of each, the rehearsal and the next schedule."""
    settings = services.settings
    if runs.reap_stale(session, settings.backup_stale_hours):
        session.commit()
    detail = principal.kind == "user"
    plan = runs.schedule(session, settings)
    pending = (
        session.query(Job.id)
        .filter(Job.kind == KIND, Job.status.in_(("pending", "running")))
        .first()
    )
    last_release = runs.latest(session, "release")
    rehearsed = (
        session.query(BackupRun.rehearsed_at)
        .filter(BackupRun.kind == "release", BackupRun.rehearsal_status == "ok")
        .order_by(BackupRun.rehearsed_at.desc())
        .first()
    )
    last_ok = rehearsed[0] if rehearsed else None
    max_days = settings.release_rehearsal_days
    overdue = last_release is not None and (
        last_ok is None or datetime.now(UTC) - last_ok > timedelta(days=max_days)
    )
    last_backup = runs.latest(session, "backup")
    return BackupsView(
        schedule=ScheduleView(
            enabled=settings.release_enabled,
            recipient_configured=bool(settings.release_age_recipient.strip()),
            interval_days=settings.release_interval_days,
            next_due=plan.next_due,
            due_now=plan.due_now,
            reason=plan.reason,
            build_pending=pending is not None,
        ),
        rehearsal=RehearsalView(last_ok_at=last_ok, overdue=overdue, max_days=max_days),
        last_backup=view(last_backup, detail=detail) if last_backup else None,
        last_release=view(last_release, detail=detail) if last_release else None,
        history=[view(r, detail=detail) for r in runs.history(session, 10)],
    )


@router.post("/backups/release", status_code=status.HTTP_202_ACCEPTED)
def build_release_now(
    services: ServicesDep, session: SessionDep, admin: AdminDep
) -> dict[str, int]:
    """Request a release now (the worker builds, encrypts, splits and checks it; it publishes
    only if `RELEASE_ENABLED=true`). Admins only."""
    try:  # cifra mesmo um byte com a chave configurada e confere a assinatura
        check_release_config(services.settings)
    except (ReleaseConfigError, AgeError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    job_id = enqueue_release(session, publish_it=True, requested_by=admin.name)
    session.commit()
    return {"job_id": job_id}


@router.post("/backups/{run_id}/rehearsal")
def record_rehearsal(
    session: SessionDep, admin: AdminDep, run_id: int, body: RehearsalBody
) -> RunView:
    """Record a restore rehearsal done outside the system (with the private key: `ariadne release
    verify … --identity`, or `restore.sh`). Admins only."""
    row = session.get(BackupRun, run_id)
    if row is None or row.kind != "release":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown release.")
    row.rehearsal_status = body.status
    row.rehearsed_at = datetime.now(UTC)
    row.rehearsal_details = {"by": admin.name, "notes": body.notes, "manual": True}
    session.commit()
    return view(row)
