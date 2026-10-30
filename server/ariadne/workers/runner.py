"""Ciclo dos workers: reclama jobs da fila, corre o handler, e faz as tarefas periódicas.

`ariadne worker --all` corre todos; `--only indexer,importer,curator,...` escolhe. Sai limpo em
SIGTERM/SIGINT: acaba o job em curso e para.
"""

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from ariadne import queue
from ariadne.compute import control as compute_control
from ariadne.compute import tasks as compute_tasks
from ariadne.compute.tick import run_compute_tick
from ariadne.config import Settings
from ariadne.mempalace_store import run_mempalace_sync
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.rooms_build import run_rooms
from ariadne.services import Services
from ariadne.wiki import refresh
from ariadne.workers import article_vector, curator, importer, indexer
from ariadne.workers import books as books_worker
from ariadne.workers import catalog as catalog_worker
from ariadne.workers import courses as courses_worker
from ariadne.workers import release as release_worker
from ariadne.workers.decay import run_decay
from ariadne.workers.linker import run_linker
from ariadne.workers.neighbors import run_neighbors

logger = logging.getLogger(__name__)

# (serviços, job, sinal de paragem): um handler longo olha para o sinal entre lotes
JobHandler = Callable[[Services, ClaimedJob, threading.Event], None]

# worker -> tipos de job que trata
JOB_WORKERS: dict[str, dict[str, JobHandler]] = {
    "indexer": {
        indexer.KIND: indexer.index_document,
        article_vector.KIND: article_vector.article_vector,
    },
    "importer": {
        importer.KIND: importer.import_page,
        importer.INDEX_KIND: importer.update_domain_index,
        refresh.CLASSIFY_KIND: importer.classify_domain_local,
    },
    "curator": {curator.KIND: curator.curate_page},
    "courses": {courses_worker.KIND: courses_worker.publish_job},
    "catalog": {catalog_worker.KIND: catalog_worker.download_job},
    "release": {release_worker.KIND: release_worker.release_job},
}
# worker -> (tarefa periódica sem fila, intervalo em segundos)
PeriodicTask = Callable[[Services, threading.Event], object]
PERIODIC_WORKERS: dict[str, tuple[PeriodicTask, Callable[[Settings], float]]] = {
    "decay": (lambda services, stop: run_decay(services), lambda s: s.decay_interval_hours * 3600),
    "neighbors": (run_neighbors, lambda s: s.neighbors_interval_seconds),
    "linker": (run_linker, lambda s: s.link_interval_seconds),
    "rooms": (run_rooms, lambda s: s.rooms_interval_seconds),
    "mempalace": (run_mempalace_sync, lambda s: s.mempalace_interval_seconds),
    "importer": (importer.run_import_scan, lambda s: s.import_interval_seconds),
    "courses": (courses_worker.run_courses, lambda s: s.courses_interval_seconds),
    "books": (books_worker.run_books, lambda s: s.books_interval_seconds),
    "catalog": (catalog_worker.sweep_stale_parts, lambda s: 3600.0),
    "release": (release_worker.run_release_schedule, lambda s: s.release_check_interval_seconds),
    # O agendador das corridas de lote em GPU externa (painel de curadoria).
    "compute": (run_compute_tick, lambda s: s.compute_tick_seconds),
}
ALL_WORKERS = sorted({*JOB_WORKERS, *PERIODIC_WORKERS})

STALE_CHECK_SECONDS = 60.0


class UnknownWorker(ValueError):
    pass


def parse_workers(only: str | None, everything: bool) -> list[str]:
    """Os nomes de `--only a,b` (validados) ou todos."""
    if everything:
        return list(ALL_WORKERS)
    names = [n.strip() for n in (only or "").split(",") if n.strip()]
    unknown = [n for n in names if n not in ALL_WORKERS]
    if unknown or not names:
        raise UnknownWorker(
            f"Unknown workers: {unknown or '(none given)'}. Available: {', '.join(ALL_WORKERS)}."
        )
    return names


@dataclass
class Runner:
    services: Services
    workers: list[str]
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.handlers: dict[str, JobHandler] = {}
        for name in self.workers:
            self.handlers.update(JOB_WORKERS.get(name, {}))
        self.periodic = {n: PERIODIC_WORKERS[n] for n in self.workers if n in PERIODIC_WORKERS}
        self._next_periodic: dict[str, float] = {n: 0.0 for n in self.periodic}
        self._next_stale_check = 0.0
        self._offline_logged = False

    def run_once(self) -> bool:
        """Reclama e trata um job. Devolve `False` se não havia nenhum devido."""
        if not self.handlers:
            return False
        with self.services.sessions() as session:
            # Os tipos que têm lote (`ariadne.compute`) obedecem ao painel: pausa, janela, destino.
            governed = set(compute_tasks.kinds())
            policy = compute_control.local_policy(session)
            kinds = [k for k in self.handlers if k not in governed]
            kinds += [k for k in policy.kinds if k in self.handlers]
            tried_only = [k for k in policy.tried_only if k in self.handlers]
            if curator.KIND in kinds + tried_only and self._curator_offline():
                # O Mac está desligado: os jobs de curadoria ficam como estão (sem gastar
                # tentativas) e voltam a ser reclamados quando ele responder.
                kinds = [k for k in kinds if k != curator.KIND]
                tried_only = [k for k in tried_only if k != curator.KIND]
            job = (
                queue.claim(session, kinds, self.worker_id, tried_only_kinds=tried_only)
                if kinds or tried_only
                else None
            )
            session.commit()
        if job is None:
            return False
        logger.info("Job %d (%s) attempt %d", job.id, job.kind, job.attempts)
        try:
            self.handlers[job.kind](self.services, job, self.stop)
        except Defer as later:
            with self.services.sessions() as session:
                queue.defer(session, job.id, later.run_after, job.worker_id, str(later))
                session.commit()
            logger.info("Job %d deferred until %s: %s", job.id, later.run_after.isoformat(), later)
        except Permanent as exc:
            self._fail(job, str(exc), permanent=True)
            logger.warning("Job %d failed for good: %s", job.id, exc)
        except Exception as exc:
            self._fail(job, f"{type(exc).__name__}: {exc}", permanent=False)
            logger.exception("Job %d failed (attempt %d)", job.id, job.attempts)
        else:
            with self.services.sessions() as session:
                queue.complete(session, job.id, job.worker_id)
                session.commit()
        return True

    def _curator_offline(self) -> bool:
        status = self.services.curator_status()
        if status.online is False:
            if not self._offline_logged:
                logger.warning("Curator offline (%s): curation is on hold", status.detail)
                self._offline_logged = True
            return True
        if self._offline_logged and status.online:
            logger.info("Curator back: curation resumes")
        self._offline_logged = False
        return False

    def _fail(self, job: ClaimedJob, error: str, *, permanent: bool) -> None:
        with self.services.sessions() as session:
            queue.fail(
                session,
                job.id,
                error,
                job.worker_id,
                max_attempts=self.services.settings.job_max_attempts,
                permanent=permanent,
            )
            session.commit()

    def housekeeping(self) -> None:
        """Recupera jobs presos e corre as tarefas periódicas que já estão devidas."""
        self.services.sync_config()  # o cofre das integrações pode ter mudado (sem reiniciar)
        settings = self.services.settings
        now = time.monotonic()
        if now >= self._next_stale_check:
            self._next_stale_check = now + STALE_CHECK_SECONDS
            with self.services.sessions() as session:
                recovered = queue.recover_stale(
                    session,
                    timedelta(minutes=settings.job_stale_minutes),
                    settings.job_max_attempts,
                )
                session.commit()
            if recovered:
                logger.warning("%d stuck jobs went back to the queue", recovered)
        for name, (task, interval) in self.periodic.items():
            if now < self._next_periodic[name]:
                continue
            self._next_periodic[name] = now + interval(settings)
            try:
                result = task(self.services, self.stop)
            except Exception:
                logger.exception("Periodic task %s failed", name)
            else:
                if getattr(result, "backlog", False) and not self.stop.is_set():
                    # Ficou trabalho por fazer: volta no ciclo seguinte, não daqui a um intervalo.
                    self._next_periodic[name] = time.monotonic() + settings.worker_poll_seconds

    def run_forever(self) -> None:
        logger.info("Worker %s running: %s", self.worker_id, ", ".join(self.workers))
        poll = self.services.settings.worker_poll_seconds
        while not self.stop.is_set():
            try:
                self.housekeeping()
                if self.run_once():
                    continue  # há mais? não se espera entre jobs
            except Exception:
                logger.exception("Error in the worker loop (the database?); trying again")
            self.stop.wait(poll)
        logger.info("Worker %s finished", self.worker_id)
