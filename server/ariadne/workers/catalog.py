"""Worker `catalog`: descarrega ZIM do catálogo do Kiwix para `ZIM_DIR` (Fase 12).

Job `catalog_download` ({book, requested_by}): resolve a entrada no catálogo (o URL nunca vem do
pedido), lê o `.meta4`, descarrega com retoma, confere tamanho e sha-256, e regista a fonte
(`sources sync`). O kiwix-serve apanha o ZIM novo sozinho (o contentor mantém o `library.xml`,
ver `deploy/kiwix`); o job espera um pouco por isso e regista se já é servido. O progresso fica
no `payload` (`phase`, `bytes`, `total`): é o que o dashboard mostra. Um pedido de cancelamento
(`cancel` no payload) pára a descarga e deixa o `.part`, que uma nova descarga retoma.
"""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne import queue
from ariadne.db.models import Job
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.services import Services
from ariadne.zimcatalog.download import (
    DownloadCancelled,
    DownloadError,
    DownloadMeta,
    FatalDownloadError,
    check_url,
    download_zim,
    is_local_url,
    resolve_meta,
)
from ariadne.zimcatalog.opds import CatalogEntry, CatalogError, EntryNotFound

logger = logging.getLogger(__name__)

KIND = "catalog_download"
CATALOG_RETRY = timedelta(minutes=2)
PROGRESS_EVERY = 1.5  # segundos entre gravações do progresso
CANCEL_EVERY = 1.0  # segundos entre leituras do pedido de cancelamento


def dedupe_key(book: str) -> str:
    return f"{KIND}:{book}"


def enqueue_download(
    session: Session, entry: CatalogEntry, requested_by: str, *, allow_unverified: bool = False
) -> int:
    """Pede a descarga de um livro do catálogo. Já pendente **ou a correr**: devolve esse job (o
    mesmo livro não se descarrega duas vezes). Sem commit."""
    existing = queue.active_by_key(session, KIND, dedupe_key(entry.id))
    if existing is not None:
        return existing
    return queue.enqueue(
        session,
        KIND,
        {
            "book": entry.id,
            "name": entry.name,
            "title": entry.title,
            "stem": entry.stem,
            "size": entry.size_bytes,
            "requested_by": requested_by,
            "allow_unverified": allow_unverified,
            "phase": "queued",
            "bytes": 0,
            "total": entry.size_bytes,
        },
        priority=0.5,
        dedupe_key=dedupe_key(entry.id),
    )


def active_downloads(session: Session) -> list[Job]:
    """Descargas à espera, a correr ou terminadas há pouco (para o dashboard)."""
    recent = datetime.now(UTC) - timedelta(hours=24)
    return list(
        session.scalars(
            select(Job)
            .where(
                Job.kind == KIND,
                (Job.status.in_((queue.PENDING, queue.RUNNING))) | (Job.created_at >= recent),
            )
            .order_by(Job.id.desc())
            .limit(50)
        )
    )


def request_cancel(session: Session, job: Job) -> str:
    """Cancela uma descarga: a pendente falha logo; a que corre pára no próximo bloco."""
    if job.status == queue.PENDING:
        job.status = queue.FAILED
        job.last_error = "cancelada"
        job.finished_at = datetime.now(UTC)
        job.payload = {**job.payload, "phase": "cancelled"}
        return "cancelled"
    if job.status == queue.RUNNING:
        queue.patch_payload(session, job.id, None, {"cancel": True})
        return "cancelling"
    return "finished"


def _wait_served(services: Services, stem: str, stop: threading.Event) -> bool:
    """Espera (até `ZIM_WAIT_SERVED_SECONDS`) que o kiwix-serve conheça o livro."""
    deadline = time.monotonic() + services.settings.zim_wait_served_seconds
    url = f"{services.settings.kiwix_url.rstrip('/')}/content/{quote(stem)}/"
    while True:
        try:
            if httpx.get(url, timeout=5, follow_redirects=False).status_code < 400:
                return True
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline or stop.wait(3.0):
            return False


def download_job(services: Services, job: ClaimedJob, stop: threading.Event) -> None:
    book = job.payload.get("book")
    if not isinstance(book, str):
        raise Permanent(f"invalid payload: {job.payload!r}")
    settings = services.settings
    catalog = services.get_catalog()
    try:
        entry = catalog.entry(book)
    except EntryNotFound as exc:
        raise Permanent(f"book {book} no longer exists in the catalog") from exc
    except CatalogError as exc:
        raise Defer(datetime.now(UTC) + CATALOG_RETRY, f"catalog unavailable: {exc}") from exc
    if not entry.download_url:
        raise Permanent("the catalog entry has no download link")
    allow_local = is_local_url(settings.kiwix_catalog_url)
    last = {"at": 0.0}

    def save(patch: dict[str, Any]) -> None:
        with services.sessions() as session:
            queue.patch_payload(session, job.id, job.worker_id, patch)
            session.commit()

    checked = {"at": 0.0}

    def cancelled() -> str | None:
        if stop.is_set():
            return "the worker is stopping"
        now = time.monotonic()
        if now - checked["at"] < CANCEL_EVERY:  # chamado a cada bloco recebido: não a cada vez
            return None
        checked["at"] = now
        with services.sessions() as session:
            payload = session.scalar(select(Job.payload).where(Job.id == job.id)) or {}
        return "cancelada" if payload.get("cancel") else None

    def progress(done: int, total: int | None, phase: str) -> None:
        now = time.monotonic()
        if now - last["at"] < PROGRESS_EVERY and phase != "done":
            return
        last["at"] = now
        save({"phase": phase, "bytes": done, "total": total})

    meta: DownloadMeta | None = None

    def drop_part() -> None:
        if meta is not None:
            (settings.zim_dir / f"{meta.filename}.part").unlink(missing_ok=True)

    try:
        with httpx.Client(
            timeout=httpx.Timeout(settings.zim_download_timeout_seconds, connect=20.0),
            headers={"User-Agent": "ariadne/0.1 (+kiwix catalog)"},
        ) as client:
            check_url(entry.download_url, allow_local=allow_local)
            save({"phase": "resolving"})
            meta = resolve_meta(client, entry.download_url, allow_local=allow_local)
            save({"phase": "downloading", "filename": meta.filename, "total": meta.size})
            path = download_zim(
                meta,
                settings.zim_dir,
                client=client,
                allow_local=allow_local,
                progress=progress,
                should_stop=cancelled,
                free_margin=settings.zim_download_free_margin_mb * 1024 * 1024,
                allow_unverified=bool(job.payload.get("allow_unverified")),
            )
    except DownloadCancelled as exc:
        if stop.is_set():  # o worker parou: o job volta à fila e retoma do `.part`
            raise Defer(datetime.now(UTC), str(exc)) from exc
        drop_part()  # cancelada de propósito: não fica lixo
        save({"phase": "cancelled"})
        raise Permanent("download cancelled") from exc
    except FatalDownloadError as exc:  # sem remédio (disco cheio, sem checksum…): nada de repetir
        drop_part()
        save({"phase": "failed", "error": str(exc)[:500]})
        raise Permanent(str(exc)) from exc
    except DownloadError as exc:
        if (
            job.attempts >= settings.job_max_attempts
        ):  # a última tentativa: o .part não serve a ninguém
            drop_part()
        save({"phase": "failed", "error": str(exc)[:500]})
        raise RuntimeError(str(exc)) from exc

    save({"phase": "registering", "filename": path.name})
    with services.sessions() as session:
        report = services.registry.sync(session)
    source_id = next(
        (
            source.id
            for sid in report.active
            if (source := services.registry.get(sid)) is not None and source.path == str(path)
        ),
        None,
    )
    if source_id is None:
        reason = report.errors.get(path.name, "was not registered")
        save({"phase": "failed", "error": reason})
        raise Permanent(f"{path.name} downloaded but does not open as a source: {reason}")
    save({"phase": "serving", "source_id": source_id})
    served = _wait_served(services, path.stem, stop)
    size = path.stat().st_size
    save({"phase": "done", "source_id": source_id, "served": served, "bytes": size})
    logger.info("ZIM %s registered as %s (served by kiwix: %s)", path.name, source_id, served)


PART_MAX_AGE = timedelta(days=3)


def sweep_stale_parts(services: Services, stop: threading.Event | None = None) -> int:
    """Tarefa periódica: apaga os `.part` abandonados (mais de 3 dias, sem descarga pendente nem a
    correr). Os de descargas falhadas ou canceladas já se apagam na hora; isto apanha o que sobrou
    de um worker que morreu."""
    folder = services.settings.zim_dir
    if not folder.is_dir():
        return 0
    with services.sessions() as session:
        names = {
            str((job.payload or {}).get("filename"))
            for job in active_downloads(session)
            if job.status in (queue.PENDING, queue.RUNNING)
        }
    removed = 0
    now = time.time()
    for part in folder.glob("*.zim.part"):
        if part.name.removesuffix(".part") in names:
            continue
        try:
            if now - part.stat().st_mtime > PART_MAX_AGE.total_seconds():
                part.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("%d abandoned .part files deleted", removed)
    return removed
