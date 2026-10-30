"""Worker `curator`: o job `curate_page` passa um artigo pelo curador e pela verificação de
fidelidade (STRUCTURE.md, secção 5, passos 7 e 8).

Corre com prioridade baixa (a curadoria acompanha o uso, não o atrasa). O resultado:

- o curador rejeitou, ou respondeu algo ilegível  -> `needs_review` (revisão humana);
- a reescrita não é fiel ao original              -> `needs_review`, com o relatório;
- a reescrita é fiel                              -> `published` e `import_page` publica.

Sem `CURATOR_URL` o job adia-se: o item continua `pending_curator` e não se publica (o nível 1
continua a servir o artigo). Se o curador não responde, adia-se sem gastar tentativas.
"""

import contextlib
import html
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from ariadne import queue
from ariadne.curation import fidelity, pipeline
from ariadne.curation import flags as flagging
from ariadne.curation.curator import (
    CURATOR_KIND,
    Curated,
    CuratorError,
    CuratorUnavailable,
    attach_fixed,
    split_fixed,
    split_parts,
)
from ariadne.db.models import Curation
from ariadne.db.models import Document as DocumentRow
from ariadne.licenses import curator_gate
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.services import Services
from ariadne.wiki.refresh import enqueue_import

logger = logging.getLogger(__name__)

KIND = CURATOR_KIND
NOT_CONFIGURED_RETRY = timedelta(minutes=10)
UNAVAILABLE_RETRY = timedelta(minutes=5)
PUBLISH_PRIORITY = 0.3


HEARTBEAT_SECONDS = 60.0  # um curador lento não deixa o job parecer preso (`job_stale_minutes`)
MUST_BE_GONE = (
    flagging.INJECTION,
    flagging.HIDDEN,
    flagging.DANGEROUS,
    flagging.ENCODED,
    flagging.INVISIBLE,
)


@contextlib.contextmanager
def _heartbeat(services: Services, job: ClaimedJob) -> Iterator[None]:
    """Renova o bloqueio do job enquanto o curador trabalha (uma parte pode demorar minutos)."""
    done = threading.Event()

    def beat() -> None:
        while not done.wait(HEARTBEAT_SECONDS):
            try:
                with services.sessions() as session:
                    queue.touch(session, job.id, job.worker_id)
                    session.commit()
            except Exception:
                logger.warning("Could not renew the lock of job %d", job.id)

    thread = threading.Thread(target=beat, name=f"curator-heartbeat-{job.id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=5)


def curate_page(services: Services, job: ClaimedJob, stop: threading.Event | None = None) -> None:
    raw = job.payload.get("document_id")
    if not isinstance(raw, str):
        raise Permanent(f"payload without document_id: {job.payload!r}")
    curator = services.get_curator()
    if curator is None:
        raise Defer(datetime.now(UTC) + NOT_CONFIGURED_RETRY, "curator not configured")
    settings = services.settings
    with services.sessions() as session:
        row = session.get(Curation, raw)
        document = session.get(DocumentRow, raw)
        if row is None or document is None or row.decision != pipeline.PENDING:
            return  # já decidido (revisão humana, texto novo): nada a fazer
        digest, original, title = row.fingerprint, row.original_md, document.title
        source_id = document.source_id
        if not curator_gate(session, settings, row):  # um job antigo: o curador deixou de servir
            session.commit()
            return
    parts = split_parts(split_fixed(original)[0], settings.curation_max_chars)
    replies: list[Curated] = []
    try:
        with _heartbeat(services, job):
            for number, part in enumerate(parts, start=1):
                if stop is not None and stop.is_set():
                    raise Defer(datetime.now(UTC), "worker stopping")
                replies.append(curator.curate(title, part, (number, len(parts))))
    except CuratorUnavailable as exc:
        services.curator_monitor.mark_down(services.settings.curator_url, str(exc))
        raise Defer(datetime.now(UTC) + UNAVAILABLE_RETRY, str(exc)) from exc
    except CuratorError as exc:
        _finish(services, raw, digest, pipeline.REVIEW, f"curador: {exc}", None, None)
        return
    conclude_curation(services, raw, digest, original, source_id, replies, curator.model)


def conclude_curation(
    services: Services,
    document_id: str,
    digest: str,
    original: str,
    source_id: str,
    replies: list[Curated],
    model: str | None,
    *,
    batch_run_id: int | None = None,
) -> str:
    """O que se faz com as respostas do curador a um artigo: o veredicto, a limpeza, a verificação
    de fidelidade e a decisão. É a mesma para o curador local e para os resultados de um lote
    externo (`batch_run_id`): em ambos, mesmo o que é fiel fica para revisão humana, a menos que
    `BATCH_AUTO_PUBLISH` o permita. Devolve a decisão gravada (`published`/`needs_review`)."""
    settings = services.settings
    external = batch_run_id is not None
    where = "external batch: " if external else ""
    if rejected := next((r for r in replies if r.verdict == "reject"), None):
        issues = "; ".join(rejected.issues[:3])
        reason = f"{where}the curator rejected it: {rejected.reason}" + (
            f" ({issues})" if issues else ""
        )
        _finish(
            services, document_id, digest, pipeline.REVIEW, reason, None,
            {"issues": rejected.issues}, model, batch_run_id,
        )  # fmt: skip
        return pipeline.REVIEW
    curated = pipeline.sanitize_curated(
        attach_fixed("\n\n".join(r.markdown.strip() for r in replies), split_fixed(original)[1]),
        pipeline.token_paths(original),
        pipeline.link_urls(original),
    )
    with services.sessions() as session:  # o título do documento não é um nome «acrescentado»
        document = session.get(DocumentRow, document_id)
        context = ""
        if document is not None:
            wiki_title = document.meta.get("wiki_title")
            context = f"{document.title} {wiki_title if isinstance(wiki_title, str) else ''}"
    report = fidelity.check(original, curated, settings.curation_fidelity_tolerance, context)
    detail = report.as_dict()
    detail["issues"] = [i for r in replies for i in r.issues][:20]
    problems = list(report.problems)
    ratio = len(curated.strip()) / max(len(original.strip()), 1)
    if not curated.strip() or ratio < settings.curation_min_length_ratio:
        problems.append(f"rewrite too short ({ratio:.0%} of the original)")
    remaining = {
        f.code
        for f in flagging.detect(html.unescape(curated), settings=settings, source_id=source_id)
    } & set(MUST_BE_GONE)
    if remaining:  # o curador devia ter tirado isto; se não tirou, não se publica
        problems.append("the rewrite still has: " + ", ".join(sorted(remaining)))
    detail["ratio"] = round(ratio, 3)
    detail["remaining_flags"] = sorted(remaining)
    detail["ok"] = not problems
    detail["problems"] = problems
    if problems:
        reason = f"{where}held: " + "; ".join(problems[:3])
        decision = pipeline.REVIEW
    elif not settings.batch_auto_publish:
        reason = f"{where}faithful to the original, awaiting validation"
        decision = pipeline.REVIEW
    else:
        reason = "curated and faithful to the original"
        decision = pipeline.PUBLISHED
    _finish(services, document_id, digest, decision, reason, curated, detail, model, batch_run_id)
    return decision


def _finish(
    services: Services,
    document_id: str,
    digest: str,
    decision: str,
    reason: str,
    curated_md: str | None,
    detail: dict[str, object] | None,
    model: str | None = None,
    batch_run_id: int | None = None,
) -> None:
    with services.sessions() as session:
        saved = pipeline.finish_curation(
            session,
            document_id,
            digest,
            decision=decision,
            reason=reason,
            model=model or services.settings.curator_model or None,
            curated_md=curated_md,
            fidelity=detail,
            batch_run_id=batch_run_id,
        )
        if saved and decision == pipeline.PUBLISHED:
            enqueue_import(session, document_id, PUBLISH_PRIORITY)
        session.commit()
    logger.info("Curation of %s: %s (%s)", document_id, decision, reason)
