"""Worker `indexer`: job `index_document` (ARCHITECTURE.md, secções 5.4 e 6).

Lê o documento pela fonte, parte-o em chunks, grava-os (idempotente por `(document_id, ord)`) e
vetoriza os que ainda não têm vetor do modelo atual, em lotes. O orçamento diário
(`INDEX_DAILY_BUDGET`, secção 6.4) limita os vetores criados por dia: esgotado, o job adia-se para
o dia seguinte e retoma de onde ficou.
"""

import logging
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariadne import queue
from ariadne.chunking import chunk_document
from ariadne.db.models import Job
from ariadne.documents import (
    LibraryError,
    document_info,
    make_document_id,
    read_article,
    register_book_candidate,
    split_document_id,
    upsert_documents,
)
from ariadne.embedder import EmbedderError
from ariadne.licenses import PrivateSendBlocked, assert_may_send, embedder_endpoint
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.services import Services
from ariadne.sources.base import NotAnArticle, NotFound, Source
from ariadne.workers.article_vector import enqueue_article_vector

logger = logging.getLogger(__name__)

KIND = "index_document"
# O modelo indisponível (sem rede, endpoint em baixo) é transitório: o job espera sem gastar
# tentativas, em vez de acabar `failed` ao fim de 5 falhas.
EMBEDDER_RETRY = timedelta(minutes=2)
# Um documento que falhou de vez (não é artigo, sumiu da fonte) não volta a ser pedido durante isto.
FAILED_COOLDOWN = timedelta(days=7)


def dedupe_key(document_id: str) -> str:
    return f"{KIND}:{document_id}"


def enqueue_index(session: Session, document_id: str, priority: float) -> int:
    """Pede a indexação de um documento; se já está pendente, só sobe a prioridade."""
    return queue.enqueue(
        session,
        KIND,
        {"document_id": document_id},
        priority=priority,
        dedupe_key=dedupe_key(document_id),
    )


def recently_failed(session: Session, document_ids: Sequence[str]) -> set[str]:
    """Dos documentos dados, os que têm um job `failed` recente (não vale a pena insistir)."""
    keys = {dedupe_key(d): d for d in document_ids}
    if not keys:
        return set()
    found = session.scalars(
        select(Job.dedupe_key).where(
            Job.dedupe_key.in_(list(keys)),
            Job.status == "failed",
            Job.finished_at > func.now() - FAILED_COOLDOWN,
        )
    )
    return {keys[key] for key in found if key in keys}


def start_of_day(now: datetime) -> datetime:
    """O «dia» do orçamento diário é o dia UTC, igual em qualquer máquina e fuso do contentor."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def index_document(
    services: Services, job: ClaimedJob, stop: threading.Event | None = None
) -> None:
    """Handler do job. Levanta `Permanent` se o documento já não existe e `Defer` se o orçamento
    do dia acabou, se o embedder está em baixo ou se o worker foi mandado parar (o job volta à
    fila sem gastar tentativa); qualquer outra exceção conta como falha (recuo e nova tentativa)."""
    try:
        _index_document(services, job, stop or threading.Event())
    except EmbedderError as exc:
        raise Defer(datetime.now(UTC) + EMBEDDER_RETRY, f"embedder unavailable: {exc}") from exc


def _remaining_budget(services: Services, session: Session, model_id: str) -> int:
    """Chunks que ainda cabem hoje; `Defer` para a meia-noite UTC se já não cabe nenhum."""
    now = datetime.now(UTC)
    used = services.store.count_vectors_since(session, model_id, start_of_day(now))
    remaining = services.settings.index_daily_budget - used
    if remaining <= 0:
        raise Defer(start_of_day(now) + timedelta(days=1), "daily indexing budget used up")
    return remaining


def _book_candidate(
    services: Services, source: Source, raw: str, path: str, exc: NotAnArticle, model_id: str
) -> bool:
    """Um item PDF/EPUB (`NotAnArticle` com tipo de livro) não falha o job: regista o documento
    como livro candidato (`register_book_candidate`) e dá-o como tratado para este modelo, para
    que a pesquisa não o volte a pedir. Devolve `False` se o item não é um livro."""
    with services.sessions() as session:
        found = register_book_candidate(session, source, path, exc)
        if found is None:
            return False
        document_id, kind = found
        for done_id in {document_id, raw}:  # o id pedido pode ser um redirect (UPDATE sem linha)
            services.store.mark_indexed(session, done_id, model_id, source.version)
        session.commit()
    logger.info("%s: %s, book candidate for Calibre (not indexed)", document_id, kind.reason)
    return True


def _index_document(services: Services, job: ClaimedJob, stop: threading.Event) -> None:
    raw = job.payload.get("document_id")
    if not isinstance(raw, str):
        raise Permanent(f"payload without document_id: {job.payload!r}")
    settings = services.settings
    try:
        source_id, path = split_document_id(raw)
    except LibraryError as exc:
        raise Permanent(str(exc)) from exc

    embedder = services.get_embedder()
    model_id = embedder.model_id
    with services.sessions() as session:
        # Orçamento do dia a zero: adia-se já, sem abrir o ZIM nem gravar nada.
        _remaining_budget(services, session, model_id)
        services.registry.sync_if_changed(session)
        session.commit()
    source = services.registry.get(source_id)
    if source is None:
        raise Permanent(f"Unknown source: {source_id!r}.")
    try:
        article = read_article(source, path)
    except NotAnArticle as exc:
        if _book_candidate(services, source, raw, path, exc, model_id):
            return  # um PDF/EPUB: livro candidato para o Calibre, não há nada a indexar
        raise Permanent(f"{raw!r} cannot be read: {exc}") from exc
    except NotFound as exc:
        raise Permanent(f"{raw!r} cannot be read: {exc}") from exc

    document_id = make_document_id(source.id, article.path)  # canónico: segue redirects
    try:
        with services.sessions() as session:
            assert_may_send(
                session,
                target="embeddings",
                url=embedder_endpoint(settings),
                allowed=settings.private_allowed_hosts,
                source_ids=[source.id],
                document_ids=[document_id],
            )
    except PrivateSendBlocked as exc:
        raise Permanent(str(exc)) from exc
    aliases = {raw} - {document_id}  # o id pedido, se era um redirect: fica marcado também
    chunks = chunk_document(article.title, article.text, settings.chunk_words)
    with services.sessions() as session:
        upsert_documents(session, [document_info(source, article)])
        services.store.put_chunks(session, document_id, chunks)
        session.commit()

    while True:
        with services.sessions() as session:
            pending = services.store.missing_vectors(session, document_id, model_id)
            if not pending:
                for done_id in (document_id, *aliases):
                    services.store.mark_indexed(session, done_id, model_id, source.version)
                # Um documento novo no índice ganha também o vetor de artigo (barato) para o linker.
                enqueue_article_vector(services, session, document_id, model_id, job.priority)
                session.commit()
                logger.info("Indexed %s: %d chunks (%s)", document_id, len(chunks), model_id)
                return
            if stop.is_set():  # a paragem espera pelo lote, não pelo documento inteiro
                raise Defer(datetime.now(UTC), "worker stopping")
            remaining = _remaining_budget(services, session, model_id)
            batch = pending[: min(settings.embedder_batch, remaining)]
            vectors = embedder.embed_passages([chunk.text for chunk in batch])
            services.store.put_vectors(
                session, model_id, {c.chunk_id: v for c, v in zip(batch, vectors, strict=True)}
            )
            session.commit()
        with services.sessions() as session:  # um documento grande não pode parecer preso
            queue.touch(session, job.id, job.worker_id)
            session.commit()
