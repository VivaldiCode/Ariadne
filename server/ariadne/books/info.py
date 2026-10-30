"""O que `read` e a pesquisa acrescentam a um capítulo: o livro, a posição, a citação e o leitor."""

from collections.abc import Sequence

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne.db import models


class NeighborChapter(BaseModel):
    document_id: str
    title: str


class BookInfo(BaseModel):
    """O livro de um capítulo e o lugar do capítulo nele. `reader_url` abre o livro no Kavita
    (preenchido quando o livro já está na biblioteca do Kavita)."""

    book_id: str
    title: str
    authors: list[str] = []
    language: str | None = None
    format: str
    chapter_position: int
    chapters: int
    chapter_path: str
    license_class: str
    license_name: str = ""
    attribution: str = ""
    reader_url: str | None = None
    start_page: int | None = None  # PDF: onde o capítulo começa
    end_page: int | None = None
    held_reason: str | None = None  # a curadoria assinalou o capítulo
    previous: NeighborChapter | None = None
    next: NeighborChapter | None = None


def reader_url(public_url: str, book: models.Book) -> str | None:
    """O URL do livro no Kavita (o leitor, ou a página da série), se já se sabe onde está."""
    if book.kavita_library_id is None or book.kavita_series_id is None:
        return None
    series = (
        f"{public_url.rstrip('/')}/library/{book.kavita_library_id}/series/{book.kavita_series_id}"
    )
    # O Kavita tem um leitor para EPUB (`book`) e outro para PDF (`pdf`).
    reader = "pdf" if book.format == "pdf" else "book"
    return f"{series}/{reader}/{book.kavita_chapter_id}" if book.kavita_chapter_id else series


def citation(document_id: str, position: int) -> str:
    """A citação de uma passagem: `book:<livro>/<capítulo>#<posição>`."""
    return f"{document_id}#{max(position, 0)}"


def book_infos(
    session: Session, document_ids: Sequence[str], public_url: str, *, neighbors: bool = False
) -> dict[str, BookInfo]:
    """document_id -> BookInfo, para os que são capítulos."""
    if not document_ids:
        return {}
    rows = session.execute(
        select(models.BookChapter, models.Book)
        .join(models.Book, models.Book.id == models.BookChapter.book_id)
        .where(models.BookChapter.document_id.in_(list(document_ids)))
    ).all()
    found: dict[str, BookInfo] = {}
    for chapter, book in rows:
        info = BookInfo(
            book_id=book.id,
            title=book.title,
            authors=list(book.authors),
            language=book.language,
            format=book.format,
            chapter_position=chapter.position,
            chapters=book.chapters,
            chapter_path=chapter.path,
            license_class=book.license_class,
            license_name=book.license_name,
            attribution=book.attribution,
            reader_url=reader_url(public_url, book),
            start_page=chapter.start_page,
            end_page=chapter.end_page,
            held_reason=chapter.held_reason,
        )
        if neighbors:
            around = {
                row.position: row
                for row in session.scalars(
                    select(models.BookChapter).where(
                        models.BookChapter.book_id == book.id,
                        models.BookChapter.position.in_(
                            [chapter.position - 1, chapter.position + 1]
                        ),
                    )
                )
            }
            before, after = around.get(chapter.position - 1), around.get(chapter.position + 1)
            if before is not None:
                info.previous = NeighborChapter(document_id=before.document_id, title=before.title)
            if after is not None:
                info.next = NeighborChapter(document_id=after.document_id, title=after.title)
        found[chapter.document_id] = info
    return found
