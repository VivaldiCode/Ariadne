"""Job `article_vector`: o vetor de artigo (título + 1.º parágrafo) de um documento tocado pelo uso.

É barato (um texto curto por documento) e por isso não gasta o orçamento diário, que conta
chunks. A carga em massa faz-se no Mac (`ariadne vectors build`); isto só cobre o que o servidor
vai indexando. O indexer enfileira-o quando acaba de indexar um documento sem vetor de artigo.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ariadne import queue
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
from ariadne.sources.base import NotAnArticle, NotFound
from ariadne.vectors import passage_text

logger = logging.getLogger(__name__)

KIND = "article_vector"
EMBEDDER_RETRY = timedelta(minutes=2)  # embedder em baixo: espera sem gastar tentativas


def dedupe_key(document_id: str) -> str:
    return f"{KIND}:{document_id}"


def enqueue_article_vector(
    services: Services, session: Session, document_id: str, model_id: str, priority: float
) -> bool:
    """Pede o vetor de artigo do documento, se ainda não o tem (deste modelo). Devolve se pediu."""
    if services.store.has_article_vectors(session, [document_id], model_id):
        return False
    queue.enqueue(
        session,
        KIND,
        {"document_id": document_id},
        priority=priority,
        dedupe_key=dedupe_key(document_id),
    )
    return True


def article_vector(
    services: Services, job: ClaimedJob, stop: threading.Event | None = None
) -> None:
    """Handler do job. `Permanent` se o documento já não se lê; `Defer` se o embedder está em
    baixo (sem gastar tentativas)."""
    raw = job.payload.get("document_id")
    if not isinstance(raw, str):
        raise Permanent(f"payload without document_id: {job.payload!r}")
    try:
        source_id, path = split_document_id(raw)
    except LibraryError as exc:
        raise Permanent(str(exc)) from exc
    with services.sessions() as session:
        services.registry.sync_if_changed(session)
        session.commit()
    source = services.registry.get(source_id)
    if source is None:
        raise Permanent(f"Unknown source: {source_id!r}.")
    try:
        article = read_article(source, path)
    except NotAnArticle as exc:
        with services.sessions() as session:
            found = register_book_candidate(session, source, path, exc)
            session.commit()
        if found is not None:
            return  # um PDF/EPUB: livro candidato para o Calibre, sem vetor de artigo
        raise Permanent(f"{raw!r} cannot be read: {exc}") from exc
    except NotFound as exc:
        raise Permanent(f"{raw!r} cannot be read: {exc}") from exc
    settings = services.settings
    try:
        with services.sessions() as session:
            assert_may_send(
                session,
                target="embeddings",
                url=embedder_endpoint(settings),
                allowed=settings.private_allowed_hosts,
                source_ids=[source.id],
                document_ids=[make_document_id(source.id, article.path)],
            )
    except PrivateSendBlocked as exc:
        raise Permanent(str(exc)) from exc
    try:
        embedder = services.get_embedder()
        [vector] = embedder.embed_passages([passage_text(article)])
    except EmbedderError as exc:
        raise Defer(datetime.now(UTC) + EMBEDDER_RETRY, f"embedder unavailable: {exc}") from exc
    document_id = make_document_id(source.id, article.path)  # canónico: segue redirects
    with services.sessions() as session:
        upsert_documents(session, [document_info(source, article)])
        services.store.put_article_vectors(session, embedder.model_id, {document_id: vector})
        session.commit()
    logger.info("Article vector of %s (%s)", document_id, embedder.model_id)
