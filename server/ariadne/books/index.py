"""Da biblioteca ao índice, aos documentos e ao grafo (STRUCTURE.md, 6.3 e 6.4).

`sync_books` percorre os livros ativos da tabela `book` e, para cada um que ainda não foi extraído
(ou cujo extrator mudou):

- extrai o ficheiro por capítulos (`BookSource`);
- `chapter`: o índice dos capítulos, com o texto extraído (`search_text`, que é exatamente o que
  `read` devolve: as posições das citações valem nos dois);
- `document`: cada capítulo é um documento (`content_type='chapter'`, a classe de licença do livro),
  por isso aparece na pesquisa, em `read` e no grafo; o linker liga-o aos artigos e às lições;
- a triagem da curadoria de cada capítulo (flags, saúde) e, se mudou, o «para relinkar».

A indexação vetorial (nível 3) **não** se pede aqui: um livro tem milhares de chunks e as
passagens entram no nível 3 pelo uso, como os artigos (`ariadne books index` pede-a à mão).
Repetir não altera nada se o livro não mudou.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ariadne.books import curation as chapter_curation
from ariadne.books.extract import EXTRACTOR_VERSION, extractor_fingerprint
from ariadne.books.source import BookSource, source_id_for
from ariadne.db import models
from ariadne.documents import DocumentInfo, make_document_id, upsert_documents
from ariadne.services import Services
from ariadne.sources.base import SourceError
from ariadne.workers.indexer import enqueue_index

logger = logging.getLogger(__name__)

# O tsvector de um capítulo tem de caber no limite do Postgres (1 MB): ~300 mil caracteres de
# texto corrido cabem com folga. Se ainda assim passar (texto com palavras todas diferentes),
# `_store_chapter` corta ao meio até caber.
SEARCH_TEXT_MAX = 300_000
MIN_SEARCH_TEXT = 10_000
MAX_ATTEMPTS = 3  # um livro que falha a indexar tantas vezes seguidas deixa de ser repetido
INDEX_PRIORITY = 0.5


@dataclass
class BookSyncReport:
    books: int = 0  # livros extraídos agora
    chapters: int = 0
    changed_chapters: int = 0
    held: int = 0  # capítulos assinalados pela curadoria
    enqueued: int = 0
    errors: list[str] = field(default_factory=list)


def document_id_of(book_id: str, path: str) -> str:
    return make_document_id(source_id_for(book_id), path)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def sync_books(
    services: Services,
    *,
    force: bool = False,
    book_ids: list[str] | None = None,
    enqueue: bool = False,
) -> BookSyncReport:
    """Extrai e indexa os livros que faltam (`force`: todos). `enqueue` pede também a indexação
    vetorial dos capítulos que mudaram."""
    report = BookSyncReport()
    with services.sessions() as session:
        services.registry.sync(session)  # as fontes `book:*` entram em `source`
        session.commit()
        query = select(
            models.Book.id,
            models.Book.extractor_version,
            models.Book.indexed_at,
            models.Book.meta,
        ).where(models.Book.status == "active")
        if book_ids:
            query = query.where(models.Book.id.in_(book_ids))
        fingerprint = extractor_fingerprint()
        todo = [
            book_id
            for book_id, version, indexed_at, meta in session.execute(
                query.order_by(models.Book.id)
            )
            if force
            or (
                (
                    indexed_at is None
                    or version != EXTRACTOR_VERSION
                    or meta.get("extractor_hash") != fingerprint  # bs4, saneamento, extração…
                )
                and int(meta.get("index_attempts", 0)) < MAX_ATTEMPTS
            )
        ]
    for book_id in todo:
        try:
            _index_book(services, book_id, report, enqueue=enqueue)
        except Exception as exc:  # um livro estragado não impede os outros
            logger.exception("Book %s: extraction failed", book_id)
            report.errors.append(f"{book_id}: {type(exc).__name__}: {exc}")
            _note_failure(services, book_id, exc)
    return report


def _note_failure(services: Services, book_id: str, exc: Exception) -> None:
    """Conta a falha (o worker pára de repetir ao fim de `MAX_ATTEMPTS`; `--force` ou `books
    sync --force` volta a tentar) e guarda o motivo no livro."""
    with services.sessions() as session:
        session.execute(
            text(
                "UPDATE book SET meta = meta || jsonb_build_object("
                " 'index_attempts', COALESCE((meta->>'index_attempts')::int, 0) + 1,"
                " 'index_error', CAST(:error AS text)) WHERE id = :id"
            ),
            {"id": book_id, "error": f"{type(exc).__name__}: {exc}"[:500]},
        )
        session.commit()


def _retire_vectors(session: Session, document_ids: list[str]) -> None:
    """Tira os chunks e os vetores de documentos cujo texto desapareceu ou mudou: as passagens
    antigas não podem aparecer no nível 3 com posições que já não valem (voltam ao indexar)."""
    if not document_ids:
        return
    ids = {"ids": document_ids}
    session.execute(
        text(
            "DELETE FROM chunk_vector WHERE chunk_id IN"
            " (SELECT id FROM chunk WHERE document_id = ANY(:ids))"
        ),
        ids,
    )
    session.execute(
        text(
            "UPDATE query_result SET chunk_id = NULL WHERE chunk_id IN"
            " (SELECT id FROM chunk WHERE document_id = ANY(:ids))"
        ),
        ids,
    )
    session.execute(text("DELETE FROM chunk WHERE document_id = ANY(:ids)"), ids)
    session.execute(text("DELETE FROM document_vector WHERE document_id = ANY(:ids)"), ids)


def _store_chapter(session: Session, document_id: str, values: dict[str, Any], body: str) -> None:
    """Grava o capítulo. Se o texto fizer um `tsvector` maior que o limite do Postgres, corta-o ao
    meio (num savepoint, que não estraga a transação) até caber."""
    cap = SEARCH_TEXT_MAX
    while True:
        values = {**values, "search_text": body[:cap]}
        stmt = insert(models.BookChapter).values(document_id=document_id, **values)
        try:
            with session.begin_nested():
                session.execute(
                    stmt.on_conflict_do_update(index_elements=["document_id"], set_=values)
                )
            return
        except DBAPIError:
            if cap <= MIN_SEARCH_TEXT:
                raise
            cap //= 2


def _index_book(services: Services, book_id: str, report: BookSyncReport, *, enqueue: bool) -> None:
    source = services.registry.get(source_id_for(book_id))
    if not isinstance(source, BookSource):
        raise SourceError(f"the source {source_id_for(book_id)} is not visible (missing file?)")
    extracted = source.content()
    paths = [chapter.path for chapter in extracted.chapters]
    changed: list[str] = []
    held = 0
    with services.sessions() as session:
        upsert_documents(
            session, [DocumentInfo(source, c.path, c.title) for c in extracted.chapters]
        )
        total_chars = 0
        for position, chapter in enumerate(extracted.chapters, start=1):
            article = source.read(chapter.path)
            body = article.text
            total_chars += len(body)
            document_id = document_id_of(book_id, chapter.path)
            digest = hashlib.sha256(f"{article.title}\n{body}".encode()).hexdigest()
            previous = session.scalar(
                select(models.Document.meta["content_hash"].astext).where(
                    models.Document.id == document_id
                )
            )
            meta = {
                "book": book_id,
                "chapter": chapter.path,
                "position": position,
                "content_hash": digest,
            }
            if previous != digest:
                changed.append(document_id)
                session.execute(
                    text(
                        "UPDATE document SET meta = (meta - 'indexed') || CAST(:meta AS jsonb),"
                        " linked_at = NULL, link_lease = NULL, link_attempts = 0 WHERE id = :id"
                    ),
                    {"meta": _json(meta), "id": document_id},
                )
            else:
                session.execute(
                    text("UPDATE document SET meta = meta || CAST(:meta AS jsonb) WHERE id = :id"),
                    {"meta": _json(meta), "id": document_id},
                )
            _row, result = chapter_curation.evaluate_chapter(
                session,
                services.settings,
                document_id=document_id,
                source_id=source.id,
                version=source.version,
                chapter=chapter,
                text=body,
            )
            held += 0 if result.held_reason is None else 1
            values = {
                "book_id": book_id,
                "position": position,
                "path": chapter.path,
                "title": chapter.title,
                "href": chapter.href,
                "start_page": chapter.start_page,
                "end_page": chapter.end_page,
                "characters": len(body),
                "content_hash": digest,
                "held_reason": result.held_reason,
                "updated_at": datetime.now(UTC),
            }
            _store_chapter(session, document_id, values, body)
        # Capítulos que já não estão no livro (outro extrator): saem do índice, o documento fica.
        keep = {document_id_of(book_id, p) for p in paths}
        gone = list(
            session.scalars(
                select(models.BookChapter.document_id).where(
                    models.BookChapter.book_id == book_id,
                    models.BookChapter.document_id.not_in(keep),
                )
            )
        )
        _retire_vectors(session, [*gone, *changed])
        if gone:
            session.execute(
                delete(models.BookChapter).where(models.BookChapter.document_id.in_(gone))
            )
            session.execute(
                update(models.Document)
                .where(models.Document.id.in_(gone))
                .values(meta=models.Document.meta.op("||")({"removed_in": source.version}))
            )
        session.execute(
            update(models.Book)
            .where(models.Book.id == book_id)
            .values(
                chapters=len(paths),
                pages=extracted.pages,
                characters=total_chars,
                extractor_version=EXTRACTOR_VERSION,
                extraction_note=extracted.note,
                indexed_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        session.execute(
            text(
                "UPDATE book SET meta = (meta - 'index_attempts' - 'index_error')"
                " || jsonb_build_object('extractor_hash', CAST(:fingerprint AS text))"
                " WHERE id = :id"
            ),
            {"id": book_id, "fingerprint": extractor_fingerprint()},
        )
        session.commit()
    report.books += 1
    report.chapters += len(paths)
    report.changed_chapters += len(changed)
    report.held += held
    if enqueue and changed:
        with services.sessions() as session:
            for document_id in changed:
                enqueue_index(session, document_id, INDEX_PRIORITY)
            session.commit()
        report.enqueued += len(changed)
    logger.info("Book %s: %d chapters (%s), %d flagged", book_id, len(paths), extracted.note, held)


def enqueue_book_index(services: Services, book_id: str, priority: float = INDEX_PRIORITY) -> int:
    """Pede a indexação vetorial (nível 3) de todos os capítulos do livro. Devolve quantos."""
    with services.sessions() as session:
        documents = list(
            session.scalars(
                select(models.BookChapter.document_id)
                .where(models.BookChapter.book_id == book_id)
                .order_by(models.BookChapter.position)
            )
        )
        for document_id in documents:
            enqueue_index(session, document_id, priority)
        session.commit()
    return len(documents)
