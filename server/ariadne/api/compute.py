"""O painel de controlo da curadoria e dos destinos de computação (módulo «Curadoria» do
dashboard). Só principais `user`: quem decide o que se gasta e o que se publica é uma pessoa.

As credenciais de cada destino vêm de variáveis de ambiente do worker. Aqui só se diz **se** estão
definidas, nunca o valor.
"""

import contextlib
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ariadne import queue
from ariadne.api.deps import AdminDep, ServicesDep, SessionDep, UserDep
from ariadne.compute import control as panel
from ariadne.compute import quota as quota_module
from ariadne.compute import runs, tasks, tick
from ariadne.compute.backends import BackendError, ComputeBackend
from ariadne.compute.gateway import issue_token
from ariadne.compute.runs import RunError
from ariadne.compute.tick import MANUAL_DEADLINE_SECONDS
from ariadne.db.models import BatchRun, ComputeControl, Curation
from ariadne.models_policy import ModelNotAllowed, assert_allowed_model
from ariadne.services import Services

router = APIRouter(tags=["compute"])
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class ComputeCredentialView(BaseModel):
    name: str
    set: bool
    required: bool


class ComputeQuotaView(BaseModel):
    weekly_hours: float
    used_hours: float
    remaining_hours: float
    period_start: datetime
    resets_at: datetime


class ComputeBackendView(BaseModel):
    id: str
    label: str
    mode: str  # local | auto | manual | reserved
    description: str
    implemented: bool
    configured: bool
    missing: list[str]
    credentials: list[ComputeCredentialView]
    config: dict[str, Any]  # modelo, horas semanais, … (nunca credenciais)
    quota: ComputeQuotaView | None
    # Só no destino «local» (o Mac): responde agora? `None` = sem CURATOR_URL, não se sabe.
    online: bool | None = None
    status: str | None = None


class ComputeKindView(BaseModel):
    kind: str
    prompt_version: str
    enabled: bool
    counts: dict[str, int]  # pending, batched, running, done, failed


class BatchRunView(BaseModel):
    id: int
    backend: str
    status: str
    manual: bool
    kinds: list[str]
    model: str | None
    task_count: int
    request_count: int
    applied_count: int
    review_count: int
    invalid_count: int
    released_count: int
    created_at: datetime
    submitted_at: datetime | None
    running_at: datetime | None
    finished_at: datetime | None
    gpu_seconds: float | None
    deadline_seconds: int | None
    error: str | None
    requested_by: str | None
    last_heartbeat_at: datetime | None
    progress: dict[str, Any]  # done, total, gpu_seconds, como o notebook os comunicou (túnel)


class ComputeGatewayView(BaseModel):
    enabled: bool  # GPU_GATEWAY_PUBLIC_URL definido: as corridas levam um token do túnel
    public_url: str | None
    access_headers: bool  # há service token do Cloudflare Access (só se diz se está definido)


class ComputeControlView(BaseModel):
    state: str
    destination: str
    fallback_local: bool
    schedule_enabled: bool
    window_start: str
    window_end: str
    timezone: str
    manual_override: bool
    disabled_kinds: list[str]
    batch_size: int
    max_run_hours: float
    external_blocked_reason: str | None
    active_now: bool  # play dado e (sem janela, dentro dela, ou arranque manual)
    in_window: bool
    window_remaining_seconds: float | None
    next_window_start: datetime | None
    local_can_work: bool  # o curador local pode reclamar tarefas agora
    updated_at: datetime
    updated_by: str | None


class ComputeReviewView(BaseModel):
    needs_review: int
    from_batch: int  # os que vieram de uma corrida externa


class ComputeOverview(BaseModel):
    control: ComputeControlView
    backends: list[ComputeBackendView]
    kinds: list[ComputeKindView]
    run: BatchRunView | None
    recent_runs: list[BatchRunView]
    review: ComputeReviewView
    gateway: ComputeGatewayView


def _run_view(run: BatchRun) -> BatchRunView:
    return BatchRunView(
        id=run.id,
        backend=run.backend,
        status=run.status,
        manual=run.manual,
        kinds=list(run.kinds),
        model=run.model,
        task_count=run.task_count,
        request_count=run.request_count,
        applied_count=run.applied_count,
        review_count=run.review_count,
        invalid_count=run.invalid_count,
        released_count=run.released_count,
        created_at=run.created_at,
        submitted_at=run.submitted_at,
        running_at=run.running_at,
        finished_at=run.finished_at,
        gpu_seconds=run.gpu_seconds,
        deadline_seconds=run.deadline_seconds,
        error=run.error,
        requested_by=run.requested_by,
        last_heartbeat_at=run.last_heartbeat_at,
        progress=dict(run.progress),
    )


def _control_view(control: ComputeControl, session: Session, now: datetime) -> ComputeControlView:
    policy = panel.local_policy(session, now)
    return ComputeControlView(
        state=control.state,
        destination=control.destination,
        fallback_local=control.fallback_local,
        schedule_enabled=control.schedule_enabled,
        window_start=control.window_start,
        window_end=control.window_end,
        timezone=control.timezone,
        manual_override=control.manual_override,
        disabled_kinds=list(control.disabled_kinds),
        batch_size=control.batch_size,
        max_run_hours=control.max_run_hours,
        external_blocked_reason=control.external_blocked_reason,
        active_now=panel.is_active(control, now),
        in_window=panel.in_window(control, now),
        window_remaining_seconds=panel.window_remaining(control, now),
        next_window_start=panel.next_window_start(control, now),
        local_can_work=bool(policy.kinds or policy.tried_only),
        updated_at=control.updated_at,
        updated_by=control.updated_by,
    )


def _backend_view(
    backend: ComputeBackend,
    control: ComputeControl,
    session: Session,
    now: datetime,
    services: Services,
) -> ComputeBackendView:
    config = tick.destination_config(control, backend)
    quota = quota_module.compute_quota(session, backend.id, config, now)
    reach = services.curator_status() if backend.mode == "local" else None
    return ComputeBackendView(
        id=backend.id,
        label=backend.label,
        mode=backend.mode,
        description=backend.description,
        implemented=backend.implemented,
        configured=backend.configured(),
        missing=backend.missing(),
        credentials=[ComputeCredentialView(**c) for c in backend.credentials()],
        config=config,
        quota=ComputeQuotaView(**vars(quota)) if quota else None,
        online=reach.online if reach else None,
        status=reach.detail if reach else None,
    )


@router.get("/compute")
def get_overview(services: ServicesDep, session: SessionDep, principal: UserDep) -> ComputeOverview:
    """Everything the panel shows: control, destinations (credentials set or not, quota), the
    task types with queue counts, the current and latest runs, and the review."""
    now = datetime.now(UTC)
    control = panel.get_control(session)
    session.flush()
    counts = queue.counts_by_kind(session)
    disabled = set(control.disabled_kinds)
    kinds = [
        ComputeKindView(
            kind=kind,
            prompt_version=task_type.prompt_version,
            enabled=kind not in disabled,
            counts=counts.get(kind, {}),
        )
        for kind in tasks.kinds()
        if (task_type := tasks.get(kind)) is not None
    ]
    current = runs.current_run(session)
    recent = session.scalars(select(BatchRun).order_by(BatchRun.id.desc()).limit(10)).all()
    needs_review = _count(session, Curation.decision == "needs_review")
    from_batch = _count(
        session, Curation.decision == "needs_review", Curation.batch_run_id.is_not(None)
    )
    result = ComputeOverview(
        control=_control_view(control, session, now),
        backends=[
            _backend_view(b, control, session, now, services)
            for b in services.get_backends().values()
        ],
        kinds=kinds,
        run=_run_view(current) if current else None,
        recent_runs=[_run_view(r) for r in recent],
        review=ComputeReviewView(needs_review=needs_review, from_batch=from_batch),
        gateway=ComputeGatewayView(
            enabled=bool(services.settings.gpu_gateway_public_url),
            public_url=services.settings.gpu_gateway_public_url or None,
            access_headers=bool(
                services.settings.gpu_gateway_access_id
                and services.settings.gpu_gateway_access_secret
            ),
        ),
    )
    session.commit()  # `get_control` pode ter criado a linha
    return result


def _count(session: Session, *conditions: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(Curation).where(*conditions)) or 0)


class ComputeControlUpdate(BaseModel):
    destination: str | None = None
    fallback_local: bool | None = None
    schedule_enabled: bool | None = None
    window_start: str | None = Field(None, description="HH:MM")
    window_end: str | None = Field(None, description="HH:MM")
    timezone: str | None = None
    disabled_kinds: list[str] | None = None
    batch_size: int | None = None
    max_run_hours: float | None = None


@router.put("/compute/control")
def update_control(
    services: ServicesDep, session: SessionDep, principal: AdminDep, body: ComputeControlUpdate
) -> ComputeControlView:
    """Change the destination, the scheduled window, the active types and the batch limits."""
    changes = body.model_dump(exclude_none=True)
    if (destination := changes.get("destination")) is not None:
        backend = services.get_backends().get(destination)
        if backend is None or not backend.implemented:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Destination unavailable: {destination}."
            )
    try:
        row = panel.update(session, str(principal.id), **changes)
    except panel.ControlError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    session.commit()
    return _control_view(row, session, datetime.now(UTC))


class ComputeStateChange(BaseModel):
    action: Literal["play", "pause", "resume", "stop"]
    ignore_window: bool = Field(
        False, description="Start now, even outside the scheduled window (until the next pause)."
    )


@router.post("/compute/state")
def change_state(
    session: SessionDep, principal: AdminDep, body: ComputeStateChange
) -> ComputeControlView:
    """Play/resume (`running`), pause (`paused`: launches and consumes nothing more; an external
    run already launched continues and is imported) or stop (`stopped`: cancels the current run
    and returns its tasks to the queue)."""
    state = {"play": "running", "resume": "running", "pause": "paused", "stop": "stopped"}[
        body.action
    ]
    panel.set_state(session, state, str(principal.id), ignore_window=body.ignore_window)
    session.commit()
    return _control_view(panel.get_control(session), session, datetime.now(UTC))


class ComputeBackendConfig(BaseModel):
    model: str | None = Field(None, max_length=200)
    accelerator: str | None = Field(None, max_length=60)
    weekly_hours: float | None = Field(None, ge=0, le=500)
    session_hours: float | None = Field(None, ge=0.25, le=48)
    reset_weekday: int | None = Field(None, ge=0, le=6)
    used_hours: float | None = Field(
        None,
        ge=0,
        le=500,
        description="Hours the destination says you have already used this week.",
    )


@router.put("/compute/backends/{backend_id}/config")
def update_backend_config(
    services: ServicesDep,
    session: SessionDep,
    principal: AdminDep,
    backend_id: str,
    body: ComputeBackendConfig,
) -> ComputeBackendView:
    """Model, quota and hours already used of a destination. Credentials are not changed here:
    they live in the vault (Integrations) or are environment variables."""
    backend = services.get_backends().get(backend_id)
    if backend is None or not backend.implemented:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown destination: {backend_id}.")
    changes = body.model_dump(exclude_none=True)
    try:
        assert_allowed_model(str(changes.get("model") or ""), "model")
    except ModelNotAllowed as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    control = panel.get_control(session, lock=True)
    current = dict(control.destinations.get(backend_id, {}))
    if "used_hours" in changes:
        current["used_hours_baseline"] = changes.pop("used_hours")
        current["used_hours_baseline_at"] = datetime.now(UTC).isoformat()
    current.update(changes)
    control.destinations = {**control.destinations, backend_id: current}
    session.commit()
    return _backend_view(backend, control, session, datetime.now(UTC), services)


# --- corridas ---------------------------------------------------------------------------------


@router.get("/compute/runs")
def list_runs(session: SessionDep, principal: UserDep, limit: int = 20) -> list[BatchRunView]:
    rows = session.scalars(select(BatchRun).order_by(BatchRun.id.desc()).limit(min(limit, 100)))
    return [_run_view(r) for r in rows]


class ComputeManualRun(BaseModel):
    backend: str = "colab"
    kinds: list[str] | None = None
    limit: int | None = Field(None, ge=1, le=5000)


@router.post("/compute/runs", status_code=status.HTTP_201_CREATED)
def create_manual_run(
    services: ServicesDep, principal: AdminDep, body: ComputeManualRun
) -> BatchRunView:
    """Build a batch for a manual destination (Colab): takes the tasks off the queue and leaves the
    package (`GET /compute/runs/{id}/package`) waiting for the results. Automatic destinations
    launch with play (and the window)."""
    backend = services.get_backends().get(body.backend)
    if backend is None or backend.mode != "manual":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Only manual destinations build batches by hand (Colab)."
        )
    with services.sessions() as session:
        control = panel.get_control(session)
        kinds = body.kinds or panel.enabled_kinds(control)
        limit = body.limit or control.batch_size
        config = tick.destination_config(control, backend)
        session.commit()
    unknown = sorted(set(kinds) - set(tasks.kinds()))
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown types: {unknown}.")
    try:
        run_id = tick.dispatch(
            services,
            backend,
            kinds,
            limit,
            MANUAL_DEADLINE_SECONDS,
            config,
            manual=True,
            who=str(principal.id),
        )
    except RunError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except BackendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        assert run is not None
        return _run_view(run)


def _get_run(session: Session, run_id: int) -> BatchRun:
    run = session.get(BatchRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} does not exist.")
    return run


@router.get("/compute/runs/{run_id}")
def get_run(session: SessionDep, principal: UserDep, run_id: int) -> BatchRunView:
    return _run_view(_get_run(session, run_id))


@router.get(
    "/compute/runs/{run_id}/package",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
def download_package(
    services: ServicesDep, session: SessionDep, principal: AdminDep, run_id: int
) -> Response:
    """The `.zip` package of a manual run, for the notebook: `tasks.jsonl` + `manifest.json`, or
    just the manifest if the tunnel is active (`GPU_GATEWAY_PUBLIC_URL`: the notebook pulls the
    requests itself). With the tunnel, each download **generates a new token** (the previous one
    stops working)."""
    run = _get_run(session, run_id)
    backend = services.get_backends().get(run.backend)
    if backend is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This run's destination no longer exists.")
    try:
        config = tick.destination_config(panel.get_control(session), backend)
        deadline = run.deadline_seconds or MANUAL_DEADLINE_SECONDS
        tunnel = None
        if services.settings.gpu_gateway_public_url:
            if run.status not in runs.IN_PROGRESS or run.status == "importing":
                raise RunError(f"Run {run.id} is '{run.status}'.")
            token = issue_token(run, timedelta(seconds=deadline))
            tunnel = runs.gateway_info(services.settings, token)
            session.commit()
        manifest = runs.build_manifest(run, deadline, config, tunnel)
        data = runs.bundle(run, manifest, slim=tunnel is not None)
    except RunError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="ariadne-batch-{run.id}.zip"'},
    )


def _unpack_upload(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """O que se carrega: o `results.jsonl` simples, ou o `ariadne_results.zip` do notebook."""
    try:
        return runs.read_results_file(data)
    except (zipfile.BadZipFile, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid file: {exc}") from exc


@router.post("/compute/runs/{run_id}/results")
async def upload_results(
    request: Request, services: ServicesDep, principal: AdminDep, run_id: int
) -> BatchRunView:
    """Upload the results of a manual run (`results.jsonl`, or the notebook zip). The import
    runs in the worker, with the same validation as the local curator; what comes in is left
    for human review."""
    declared = int(request.headers.get("content-length") or 0)
    if declared > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large.")
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large.")
    return await run_in_threadpool(_store, services, run_id, data)


def _store(services: Services, run_id: int, data: bytes) -> BatchRunView:
    results, meta = _unpack_upload(data)
    with services.sessions() as session:
        run = session.get(BatchRun, run_id, with_for_update=True)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} does not exist.")
        try:
            runs.store_results(session, run, results, meta)
        except RunError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        reported = meta.get("gpu_seconds")
        if isinstance(reported, int | float):
            run.gpu_seconds = float(reported)
        session.commit()
        return _run_view(run)


@router.post("/compute/runs/{run_id}/cancel")
def cancel_run(
    services: ServicesDep, session: SessionDep, principal: AdminDep, run_id: int
) -> BatchRunView:
    """Cancel a run in progress: its tasks return to the queue and any results that arrive later
    are ignored. On Kaggle the kernel cannot be cancelled through the API: stop it on the Kaggle
    page if needed."""
    run = session.get(BatchRun, run_id, with_for_update=True)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} does not exist.")
    if run.status not in runs.IN_PROGRESS or run.status == "importing":
        raise HTTPException(status.HTTP_409_CONFLICT, f"The run is '{run.status}'.")
    backend = services.get_backends().get(run.backend)
    if backend is not None:
        with contextlib.suppress(Exception):  # melhor esforço; a corrida cancela-se na mesma
            backend.cancel(dict(run.external_ref))
    runs.finish_run(session, run, "cancelled", f"cancelled by {principal.id}", tried=False)
    session.commit()
    return _run_view(run)
