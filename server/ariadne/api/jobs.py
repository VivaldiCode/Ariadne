"""Estado das filas e do orçamento diário (ARCHITECTURE.md, secção 11.2)."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func

from ariadne import queue, router
from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.db.models import Job
from ariadne.workers.indexer import start_of_day

jobs_router = APIRouter(tags=["queues"])
NEXT_JOBS_DEFAULT = 20
NEXT_JOBS_MAX = 200


class BudgetInfo(BaseModel):
    daily: int  # chunks vetorizados por dia (INDEX_DAILY_BUDGET)
    model_id: str | None  # modelo cujos vetores se contam; `None` sem embedder
    used: int | None  # vetores criados hoje (UTC); `None` sem embedder
    remaining: int | None
    resets_at: datetime  # próxima meia-noite UTC


class JobInfo(BaseModel):
    id: int
    kind: str
    priority: float
    document_id: str | None
    run_after: datetime
    due: bool  # já pode ser servido (`run_after` passou)
    attempts: int
    last_error: str | None
    status: str = "pending"
    finished_at: datetime | None = None


class JobsOverview(BaseModel):
    counts: dict[str, dict[str, int]]  # tipo -> estado -> quantos
    budget: BudgetInfo
    next: list[JobInfo]
    failed: list[JobInfo] = []  # os últimos que falharam de vez (`POST /jobs/{id}/retry`)


@jobs_router.get("/jobs")
def get_jobs(
    services: ServicesDep,
    session: SessionDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=NEXT_JOBS_MAX, description="Next jobs.")] = (
        NEXT_JOBS_DEFAULT
    ),
    failed_limit: Annotated[
        int, Query(ge=0, le=NEXT_JOBS_MAX, description="Latest failed jobs.")
    ] = NEXT_JOBS_DEFAULT,
) -> JobsOverview:
    """Counts by type/state, the day's budget (used/remaining) and the next jobs. `last_error` is
    visible only to `user` principals."""
    now = datetime.now(UTC)
    is_user = principal.kind == "user"  # as mensagens de erro (caminhos, SQL) são para pessoas
    daily = services.settings.index_daily_budget
    model_id = router.current_model_id(services)
    used = (
        services.store.count_vectors_since(session, model_id, start_of_day(now))
        if model_id is not None
        else None
    )
    db_now = session.scalar(func.now()) or now
    return JobsOverview(
        counts=queue.counts_by_kind(session),
        budget=BudgetInfo(
            daily=daily,
            model_id=model_id,
            used=used,
            remaining=max(0, daily - used) if used is not None else None,
            resets_at=start_of_day(now) + timedelta(days=1),
        ),
        next=[_info(job, db_now, show_error=is_user) for job in queue.upcoming(session, limit)],
        failed=[
            _info(job, db_now, show_error=is_user) for job in queue.failed(session, failed_limit)
        ]
        if failed_limit
        else [],
    )


def _info(job: Job, db_now: datetime, *, show_error: bool = True) -> JobInfo:
    return JobInfo(
        id=job.id,
        kind=job.kind,
        priority=job.priority,
        document_id=job.payload.get("document_id"),
        run_after=job.run_after,
        due=job.run_after <= db_now,
        attempts=job.attempts,
        last_error=job.last_error if show_error else None,
        status=job.status,
        finished_at=job.finished_at,
    )


@jobs_router.post("/jobs/{job_id}/retry")
def retry_job(session: SessionDep, principal: AdminDep, job_id: int) -> JobInfo:
    """Put a permanently failed job back in the queue (attempts start over). `user` principals
    only. 404 if it does not exist; 409 if it has not failed or an identical one is already
    queued."""
    outcome = queue.retry(session, job_id)
    if outcome == "missing":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Job {job_id} does not exist.")
    if outcome == "not_failed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Job {job_id} has not failed.")
    if outcome == "duplicate":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A pending job with the same key already exists: it covers this one.",
        )
    session.commit()
    job = session.get(Job, job_id)
    assert job is not None
    return _info(job, session.scalar(func.now()) or datetime.now(UTC))
