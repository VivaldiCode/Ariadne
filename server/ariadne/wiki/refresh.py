"""Pôr páginas do Wiki.js na fila para serem reescritas quando o que as rodeia muda."""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne import queue
from ariadne.db.models import Document as DocumentRow

KIND = "import_page"
CLASSIFY_KIND = "domain_classify"  # o lote LLM escolhe o domínio do que ficou sem certeza
CLASSIFY_PRIORITY = 0.01
INDEX_KIND = "wiki_domain_index"  # reescreve a página de um domínio num locale
INDEX_PRIORITY = 0.02  # as páginas de domínio esperam: servem a navegação, não a pesquisa
REFRESH_PRIORITY = 0.5  # uma ligação nova pesa mais do que o arranque a frio, menos do que o uso


def dedupe_key(document_id: str) -> str:
    return f"{KIND}:{document_id}"


def enqueue_import(
    session: Session, document_id: str, priority: float, *, force: bool = False
) -> int:
    """Pede a (re)importação de um documento. Sem commit; repetir não duplica. Só os artigos vão
    para o Wiki.js: as lições (cursos) e os capítulos (livros) têm a sua própria plataforma."""
    content_type = session.scalar(
        select(DocumentRow.content_type).where(DocumentRow.id == document_id)
    )
    if content_type is not None and content_type != "article":
        return 0
    payload: dict[str, object] = {"document_id": document_id}
    if force:
        payload["force"] = True
    return queue.enqueue(
        session, KIND, payload, priority=priority, dedupe_key=dedupe_key(document_id)
    )


def enqueue_refresh(session: Session, document_ids: Iterable[str]) -> int:
    """Dos documentos dados, pede a reescrita dos que já têm página (as suas «Ligações» mudaram).
    Devolve quantos pediu. Sem commit: vai na transação de quem mudou a aresta."""
    ids = sorted(set(document_ids))
    if not ids:
        return 0
    imported = session.scalars(
        select(DocumentRow.id)
        .where(DocumentRow.id.in_(ids), DocumentRow.wiki_page_id.is_not(None))
        .order_by(DocumentRow.id)
    ).all()
    for document_id in imported:
        enqueue_import(session, document_id, REFRESH_PRIORITY)
    return len(imported)


def index_dedupe_key(locale: str, domain_id: str) -> str:
    return f"{INDEX_KIND}:{locale}:{domain_id}"


def enqueue_domain_index(session: Session, locale: str, domain_id: str) -> int:
    """Pede a reescrita da página do domínio (e a das que a precedem, que a listam) no locale.
    Sem commit; os pedidos repetidos antes de o worker correr fundem-se num só."""
    return queue.enqueue(
        session,
        INDEX_KIND,
        {"locale": locale, "domain_id": domain_id},
        priority=INDEX_PRIORITY,
        dedupe_key=index_dedupe_key(locale, domain_id),
    )


def classify_dedupe_key(document_id: str) -> str:
    return f"{CLASSIFY_KIND}:{document_id}"


def enqueue_domain_classify(session: Session, document_id: str) -> int:
    """Pede ao lote LLM o domínio de um documento que ficou sem certeza (`needs_review`). Sem
    commit; repetir não duplica. O handler local não decide nada: o job espera pelo lote."""
    return queue.enqueue(
        session,
        CLASSIFY_KIND,
        {"document_id": document_id},
        priority=CLASSIFY_PRIORITY,
        dedupe_key=classify_dedupe_key(document_id),
    )
