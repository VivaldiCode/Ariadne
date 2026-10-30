"""O agendador das corridas de lote (tarefa periódica `compute` do worker).

A cada volta (`COMPUTE_TICK_SECONDS`):

1. faz avançar a corrida em curso: vigia o destino, recolhe o resultado, importa-o, ou fecha-a
   (falhou, expirou, cancelada pela paragem);
2. decide se o destino externo está *bloqueado* (sem credenciais, sem quota, falhas seguidas) e
   escreve-o em `compute_control.external_blocked_reason`: é isso que abre o curador local;
3. se está ativo (play, dentro da janela), o destino é automático e há tarefas por enviar, lança
   uma corrida nova com um prazo que cabe na janela, na sessão do destino e na quota.

Pausar não interrompe uma corrida já lançada (o Kaggle não deixa pausar): deixa de lançar novas e
os resultados da que está a correr continuam a ser recolhidos. Parar cancela-a.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from ariadne.compute import control as panel
from ariadne.compute import gateway, runs
from ariadne.compute import quota as quota_module
from ariadne.compute.backends import BackendError, ComputeBackend, RunSpec
from ariadne.compute.runs import RunError
from ariadne.db.models import BatchRun, ComputeControl
from ariadne.services import Services

logger = logging.getLogger(__name__)

LOCK_KEY = 0x41524941  # «ARIA»: um só agendador de cada vez, mesmo com vários workers
MIN_RUN_HOURS = 0.25
SETUP_SECONDS = 15 * 60  # instalar o vLLM e carregar o modelo: o notebook desconta isto do prazo
MANUAL_DEADLINE_SECONDS = 12 * 3600  # o prazo de um lote manual (Colab)
GRACE_SECONDS = 30 * 60  # depois do prazo, o destino ainda tem este tempo para devolver o output
FOREIGN_OUTPUT_SECONDS = 10 * 60  # um output que não é desta corrida pode ser o da anterior
FAILURES_BEFORE_BLOCK = 3
RETRY_AFTER_FAILURE = timedelta(minutes=10)


def destination_config(control: ComputeControl, backend: ComputeBackend) -> dict[str, Any]:
    """A configuração do destino: os valores por omissão (ambiente) com o que o painel mudou."""
    return {**backend.default_config(), **dict(control.destinations.get(backend.id, {}))}


def run_compute_tick(services: Services, stop: threading.Event | None = None) -> None:
    with services.sessions() as lock:
        if not lock.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))):
            return
        try:
            tick(services)
        finally:
            lock.scalar(select(func.pg_advisory_unlock(LOCK_KEY)))


def tick(services: Services, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    with services.sessions() as session:
        panel.get_control(session)
        session.commit()
        run = runs.current_run(session)
        run_id = run.id if run else None
    if run_id is not None:
        _advance(services, run_id, now)
    _gate_and_dispatch(services, now)


# --- fazer avançar a corrida em curso ---------------------------------------------------------


def _advance(services: Services, run_id: int, now: datetime) -> None:
    backends = services.get_backends()
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        if run is None:
            return
        status, backend_id, ref = run.status, run.backend, dict(run.external_ref)
        stopped = panel.get_control(session).state == "stopped"
        submitted = run.submitted_at or run.created_at
        deadline = run.deadline_seconds or 0
        created = run.created_at
    backend = backends.get(backend_id)
    if status == "importing":
        _import(services, run_id, backend, ref)
        return
    if status == "created":
        if now - created > timedelta(minutes=5):  # morreu entre exportar e enviar
            _close(services, run_id, "failed", "the upload never finished", tried=False)
        return
    if stopped:
        if backend is not None:
            _best_effort(backend.cancel, ref)
            _best_effort(backend.cleanup, ref)
        _close(services, run_id, "cancelled", "parado no painel", tried=False)
        return
    # O prazo vale para todas as corridas em curso: também a manual (Colab) esquecida à espera do
    # ficheiro e a de um destino que entretanto deixou de existir; senão ficavam `batched` para
    # sempre e bloqueavam as corridas seguintes.
    if now - submitted > timedelta(seconds=(deadline or MANUAL_DEADLINE_SECONDS) + GRACE_SECONDS):
        if backend is not None:
            _best_effort(backend.cancel, ref)
            _best_effort(backend.cleanup, ref)
        # Se o notebook já tinha enviado resultados pelo túnel, aproveitam-se; senão falha.
        _salvage_or_close(services, run_id, "the run did not finish in time", backend, ref)
        return
    if status == "awaiting_results" or backend is None:
        return
    try:
        polled = backend.poll(ref)
    except BackendError as exc:
        logger.warning("Run %d: could not check the status (%s)", run_id, exc)
        return
    if polled.state in ("queued", "running"):
        _note_progress(services, run_id, polled.state, now)
    elif polled.state == "failed":
        _best_effort(backend.cleanup, ref)
        _salvage_or_close(
            services, run_id, f"the target reported an error: {polled.message}", backend, ref
        )
    else:
        _collect(services, run_id, backend, ref, now, submitted)


def _note_progress(services: Services, run_id: int, state: str, now: datetime) -> None:
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        if run is None or run.status in ("importing", "done"):
            return
        if not (state == "queued" and run.status == "running"):
            run.status = state
        if state == "running" and run.running_at is None:
            run.running_at = now
        session.commit()


def _collect(
    services: Services,
    run_id: int,
    backend: ComputeBackend,
    ref: dict[str, Any],
    now: datetime,
    submitted: datetime,
) -> None:
    try:
        fetched = backend.fetch(ref)
    except BackendError as exc:
        logger.warning("Run %d: the output is not available yet (%s)", run_id, exc)
        if now - submitted > timedelta(minutes=30):
            _salvage_or_close(services, run_id, f"no output: {exc}", backend, ref)
        return
    with services.sessions() as session:
        run = session.get(BatchRun, run_id, with_for_update=True)
        if run is None or run.status in ("importing", "done"):
            return
        try:
            runs.store_results(session, run, fetched.results_jsonl, fetched.meta)
        except RunError as exc:
            if now - submitted < timedelta(seconds=FOREIGN_OUTPUT_SECONDS):
                return  # provavelmente ainda o output da corrida anterior
            run.error = str(exc)
            session.commit()
            _salvage_or_close(services, run_id, str(exc), backend, ref)
            return
        started = run.running_at or run.submitted_at or run.created_at
        observed = max(0.0, (now - started).total_seconds())
        reported = fetched.meta.get("gpu_seconds") or run.progress.get("gpu_seconds")
        run.gpu_seconds = (
            max(observed, float(reported)) if isinstance(reported, int | float) else observed
        )
        run.finished_at = now
        session.commit()
    _import(services, run_id, backend, ref)


def _import(
    services: Services, run_id: int, backend: ComputeBackend | None, ref: dict[str, Any]
) -> None:
    counts = runs.import_run(services, run_id)
    logger.info("Run %d imported: %s", run_id, counts)
    if backend is not None:
        _best_effort(backend.cleanup, ref)


def _close(services: Services, run_id: int, status: str, error: str, *, tried: bool) -> None:
    with services.sessions() as session:
        run = session.get(BatchRun, run_id, with_for_update=True)
        if run is None or run.status in ("done", "failed", "cancelled"):
            return
        runs.finish_run(session, run, status, error, tried=tried)
        session.commit()
    logger.warning("Run %d ended as %s: %s", run_id, status, error)


def _salvage_or_close(
    services: Services,
    run_id: int,
    error: str,
    backend: ComputeBackend | None,
    ref: dict[str, Any],
) -> None:
    """A corrida falhou ou expirou: se o notebook já tinha enviado resultados pelo túnel,
    aproveitam-se (importa-se o que há; o resto volta à fila como «já tentado»); senão falha."""
    with services.sessions() as session:
        run = session.get(BatchRun, run_id, with_for_update=True)
        if run is None or run.status in ("done", "failed", "cancelled", "importing"):
            return
        if run.results_gz is not None:
            reported = run.progress.get("gpu_seconds")
            started = run.running_at or run.submitted_at or run.created_at
            observed = max(0.0, (datetime.now(UTC) - started).total_seconds())
            run.gpu_seconds = max(observed, float(reported)) if reported else observed
            run.status, run.error = "importing", error[:1000]
            run.finished_at = run.finished_at or datetime.now(UTC)
            gateway.revoke_token(run)
            session.commit()
            logger.warning(
                "Run %d: %s; importing what the tunnel had already received", run_id, error
            )
        else:
            run = None
    if run is not None:
        _import(services, run_id, backend, ref)
        return
    _close(services, run_id, "failed", error, tried=True)


def _best_effort(action: Any, ref: dict[str, Any]) -> None:
    try:
        action(ref)
    except Exception as exc:
        logger.info("Target cleanup failed (ignored): %s", exc)


# --- bloquear o destino e lançar corridas ------------------------------------------------------


def blocked_reason(
    services: Services, backend: ComputeBackend | None, control: ComputeControl, now: datetime
) -> str | None:
    """Porque é que o destino externo não serve agora (e o local pode ficar com o trabalho)."""
    if control.destination == panel.LOCAL:
        return None
    if backend is None or not backend.implemented:
        return "target unavailable"
    if backend.mode != "auto":
        return None  # manual: o dono gera os lotes; o local só trata o que já foi a um lote
    if not backend.configured():
        return "missing credentials: " + ", ".join(backend.missing())
    with services.sessions() as session:
        config = destination_config(control, backend)
        quota = quota_module.compute_quota(session, backend.id, config, now)
        if quota is not None and quota.remaining_hours < MIN_RUN_HOURS:
            return "weekly GPU quota used up"
        recent = session.scalars(
            select(BatchRun.status)
            .where(BatchRun.backend == backend.id)
            .order_by(BatchRun.id.desc())
            .limit(FAILURES_BEFORE_BLOCK)
        ).all()
        if len(recent) == FAILURES_BEFORE_BLOCK and all(s == "failed" for s in recent):
            return f"the last {FAILURES_BEFORE_BLOCK} runs failed"
    return None


def _gate_and_dispatch(services: Services, now: datetime) -> None:
    backends = services.get_backends()
    with services.sessions() as session:
        control = panel.get_control(session, lock=True)
        backend = backends.get(control.destination)
        reason = blocked_reason(services, backend, control, now)
        if control.external_blocked_reason != reason:
            control.external_blocked_reason = reason
        active = panel.is_active(control, now)
        kinds = panel.enabled_kinds(control)
        limit, destination = control.batch_size, control.destination
        window = None if control.manual_override else panel.window_remaining(control, now)
        config = destination_config(control, backend) if backend else {}
        max_hours = control.max_run_hours
        session.commit()
        if (
            backend is None
            or reason is not None
            or not active
            or backend.mode != "auto"
            or runs.current_run(session) is not None
            or runs.untried_pending(session, kinds) == 0
        ):
            return
        last = session.scalars(
            select(BatchRun).where(BatchRun.backend == destination).order_by(BatchRun.id.desc())
        ).first()
        if (
            last
            and last.status == "failed"
            and now - (last.finished_at or now) < RETRY_AFTER_FAILURE
        ):
            return
        hours = min(max_hours, float(config.get("session_hours") or max_hours))
        quota = quota_module.compute_quota(session, backend.id, config, now)
        if quota is not None:
            hours = min(hours, quota.remaining_hours)
        if window is not None:
            hours = min(hours, window / 3600)
        if hours < MIN_RUN_HOURS:
            return
    try:
        dispatch(services, backend, kinds, limit, int(hours * 3600), config, manual=False, who=None)
    except RunError as exc:
        logger.info("Nothing was sent: %s", exc)
    except BackendError as exc:
        logger.warning("Sending to target %s failed: %s", backend.id, exc)


def dispatch(
    services: Services,
    backend: ComputeBackend,
    kinds: list[str],
    limit: int,
    deadline_seconds: int,
    config: dict[str, Any],
    *,
    manual: bool,
    who: str | None,
) -> int:
    """Exporta as tarefas e envia-as ao destino. Devolve o id da corrida. `RunError` se não há
    nada para enviar ou já há uma corrida; `BackendError` (a corrida fica `failed`) se o destino
    recusou."""
    with services.sessions() as session:
        run = runs.create_run(
            services,
            session,
            backend=backend.id,
            kinds=kinds,
            limit=limit,
            model=config.get("model"),
            requested_by=who,
            manual=manual,
            deadline_seconds=deadline_seconds,
        )
        if run is None:
            session.commit()  # guarda a marca dos jobs que não seguem em lote
            raise RunError("Nothing to send: no pending tasks can go in a batch.")
        tunnel = None
        if services.settings.gpu_gateway_public_url and not manual:
            token = gateway.issue_token(run, timedelta(seconds=deadline_seconds))
            tunnel = runs.gateway_info(services.settings, token)
        manifest = runs.build_manifest(
            run, max(60, deadline_seconds - SETUP_SECONDS), config, tunnel
        )
        spec = RunSpec(run.id, runs.tasks_jsonl(run), manifest, deadline_seconds, config)
        run_id = run.id
        session.commit()
    try:
        ref = backend.submit(spec)
    except BackendError as exc:
        _close(services, run_id, "failed", str(exc), tried=False)
        raise
    with services.sessions() as session:
        sent = session.get(BatchRun, run_id)
        assert sent is not None
        sent.external_ref = ref
        sent.submitted_at = datetime.now(UTC)
        sent.status = "awaiting_results" if backend.mode == "manual" else "queued"
        session.commit()
    logger.info("Run %d sent to %s", run_id, backend.id)
    return run_id
