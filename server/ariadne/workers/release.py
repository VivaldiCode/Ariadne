"""Worker `release`: a release externa semanal (Fase 12).

- tarefa periódica: se `RELEASE_ENABLED=true`, há chave pública e a última release boa tem mais de
  `RELEASE_INTERVAL_DAYS` (7), enfileira o job `release_build`. **Desligada por omissão**: sem
  `RELEASE_ENABLED` não agenda nada (construir à mão, `ariadne release run`, continua possível);
- job `release_build` ({publish, requested_by}): constrói, cifra, parte e confere a release e,
  se `publish` e `RELEASE_ENABLED`, publica-a no GitHub. Só corre uma de cada vez (chave de
  deduplicação). Um erro de configuração ou o bloqueio por licença falham de vez; uma falha de rede
  repete com recuo.
"""

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ariadne import queue
from ariadne.licenses import LicenseBlocked
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.release import runs
from ariadne.release.age import AgeError
from ariadne.release.publish import PublishError
from ariadne.release.run import (
    BuildResult,
    ReleaseConfigError,
    build_release,
    publish_built,
    release_dir,
)
from ariadne.release.sign import SignError
from ariadne.release.sink import Interrupted
from ariadne.release.verify import check_fingerprints, load_manifest
from ariadne.services import Services

logger = logging.getLogger(__name__)

KIND = "release_build"
PRIORITY = 0.2


def enqueue_release(session: Session, *, publish_it: bool, requested_by: str | None) -> int:
    """Pede uma release. Já pendente **ou a correr**: devolve esse job (a mesma release não corre
    duas vezes ao mesmo tempo). Sem commit."""
    existing = queue.active_by_key(session, KIND, KIND)
    if existing is not None:
        return existing
    return queue.enqueue(
        session,
        KIND,
        {"publish": publish_it, "requested_by": requested_by},
        priority=PRIORITY,
        dedupe_key=KIND,
    )


def run_release_schedule(services: Services, stop: threading.Event | None = None) -> None:
    """O agendamento semanal: só enfileira quando a release está devida e está tudo ligado."""
    with services.sessions() as session:
        if runs.reap_stale(session, services.settings.backup_stale_hours):
            session.commit()
        due = runs.schedule(session, services.settings)
        if not due.due_now:
            return
        enqueue_release(session, publish_it=True, requested_by="agendamento")
        session.commit()
    logger.info("Weekly release due: job %s enqueued", KIND)


def _reusable(services: Services, job: ClaimedJob) -> Path | None:
    """A pasta já construída por uma tentativa anterior deste job (e ainda boa), para uma falha de
    publicação não reconstruir tudo (a mesma tag)."""
    name = job.payload.get("built")
    if not isinstance(name, str):
        return None
    folder = release_dir(services.settings) / name
    if not folder.is_dir():
        return None
    report = check_fingerprints(folder, services.settings.release_signing_pubkey)
    return folder if report.ok else None


def release_job(services: Services, job: ClaimedJob, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    publish_it = bool(job.payload.get("publish")) and services.settings.release_enabled
    requested_by = job.payload.get("requested_by")
    try:
        folder = _reusable(services, job)
        built: BuildResult | None = None
        if folder is None:
            built = build_release(
                services, requested_by=str(requested_by) if requested_by else None, stop=stop
            )
            folder = built.directory
            with services.sessions() as session:  # uma falha a seguir não reconstrói
                queue.patch_payload(session, job.id, job.worker_id, {"built": folder.name})
                session.commit()
        elif publish_it:
            logger.info(
                "Reusing the release already built, %s (only publishing is left)", folder.name
            )
        outcome = publish_built(services, folder, dry_run=False) if publish_it else None
    except Interrupted as exc:  # o worker está a parar: o job volta à fila
        raise Defer(datetime.now(UTC), str(exc)) from exc
    except (ReleaseConfigError, SignError, AgeError) as exc:
        raise Permanent(str(exc)) from exc
    except LicenseBlocked as exc:
        raise Permanent(f"release refused by the license check: {exc}") from exc
    except PublishError as exc:
        raise RuntimeError(str(exc)) from exc
    run_id = load_manifest(folder).get("run_id")
    logger.info(
        "Release %s (run %s)%s",
        folder.name,
        run_id,
        f", published at {outcome.url}" if outcome and outcome.url else ", not published",
    )
