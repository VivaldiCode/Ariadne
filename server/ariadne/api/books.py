"""Livros (módulo «Livros» da app): a biblioteca, os capítulos extraídos e a licença."""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariadne import licenses
from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.books import index as book_index
from ariadne.books.info import reader_url
from ariadne.db import models
from ariadne.documents import LibraryError
from ariadne.sources.license import LicenseClass

router = APIRouter(tags=["books"])


class BookNotFound(LibraryError):
    pass


class BookView(BaseModel):
    id: str
    source_id: str
    title: str
    authors: list[str]
    language: str | None
    publisher: str | None
    published: str | None
    format: str
    chapters: int
    pages: int | None
    characters: int
    license_class: str
    license_name: str
    attribution: str
    status: str
    extraction_note: str | None
    calibre_id: int | None
    reader_url: str | None  # o livro no Kavita; None enquanto o Kavita não o leu
    held_chapters: int  # capítulos assinalados pela curadoria
    indexed_chapters: int  # capítulos já no nível 3 (vetores)
    added_at: datetime
    indexed_at: datetime | None


class ChapterView(BaseModel):
    document_id: str
    position: int
    title: str
    characters: int
    start_page: int | None
    end_page: int | None
    held_reason: str | None
    license_class: str
    indexed: bool  # tem vetores (nível 3)


class BookDetail(BookView):
    description: str
    identifiers: dict[str, str]
    chapter_list: list[ChapterView]


class IndexRequested(BaseModel):
    book_id: str
    chapters: int


class LicenseChange(BaseModel):
    license_class: str
    confirm: bool = False  # tornar um livro «open» pede confirmação explícita


def _view(session: Session, services: ServicesDep, row: models.Book) -> BookView:
    held, indexed = session.execute(
        select(
            func.count().filter(models.BookChapter.held_reason.is_not(None)),
            func.count().filter(models.Document.meta.has_key("indexed")),
        )
        .select_from(models.BookChapter)
        .join(models.Document, models.Document.id == models.BookChapter.document_id)
        .where(models.BookChapter.book_id == row.id)
    ).one()
    return BookView(
        id=row.id,
        source_id=f"book:{row.id}",
        title=row.title,
        authors=list(row.authors),
        language=row.language,
        publisher=row.publisher,
        published=row.published,
        format=row.format,
        chapters=row.chapters,
        pages=row.pages,
        characters=row.characters,
        license_class=row.license_class,
        license_name=row.license_name,
        attribution=row.attribution,
        status=row.status,
        extraction_note=row.extraction_note,
        calibre_id=row.calibre_id,
        reader_url=reader_url(services.settings.kavita_public_url, row),
        held_chapters=int(held),
        indexed_chapters=int(indexed),
        added_at=row.added_at,
        indexed_at=row.indexed_at,
    )


def _get(session: Session, book_id: str) -> models.Book:
    row = session.get(models.Book, book_id)
    if row is None:
        raise BookNotFound(f"Unknown book: {book_id!r} (see GET /api/books).")
    return row


@router.get("/books")
def list_books(
    session: SessionDep, services: ServicesDep, principal: PrincipalDep
) -> list[BookView]:
    """The library books (Calibre), with their license class and place in the reader (Kavita)."""
    rows = session.scalars(
        select(models.Book).where(models.Book.status == "active").order_by(models.Book.title)
    ).all()
    return [_view(session, services, row) for row in rows]


@router.get("/books/{book_id}")
def get_book(
    book_id: str, session: SessionDep, services: ServicesDep, principal: PrincipalDep
) -> BookDetail:
    """One book: metadata and the extracted chapters."""
    row = _get(session, book_id)
    chapters = session.execute(
        select(models.BookChapter, models.Document.license_class, models.Document.meta)
        .join(models.Document, models.Document.id == models.BookChapter.document_id)
        .where(models.BookChapter.book_id == book_id)
        .order_by(models.BookChapter.position)
    ).all()
    return BookDetail(
        **_view(session, services, row).model_dump(),
        description=row.description,
        identifiers={str(k): str(v) for k, v in row.identifiers.items()},
        chapter_list=[
            ChapterView(
                document_id=chapter.document_id,
                position=chapter.position,
                title=chapter.title,
                characters=chapter.characters,
                start_page=chapter.start_page,
                end_page=chapter.end_page,
                held_reason=chapter.held_reason,
                license_class=license_class,
                indexed="indexed" in meta,
            )
            for chapter, license_class, meta in chapters
        ],
    )


@router.post("/books/sync")
def sync_books(services: ServicesDep, principal: AdminDep) -> dict[str, int]:
    """Extract and index the books still missing (the `books` worker does this too)."""
    report = book_index.sync_books(services)
    return {
        "books": report.books,
        "chapters": report.chapters,
        "held": report.held,
        "errors": len(report.errors),
    }


@router.post("/books/{book_id}/index", status_code=202)
def index_book(
    book_id: str, session: SessionDep, services: ServicesDep, principal: AdminDep
) -> IndexRequested:
    """Request vector indexing (level 3) of all the book's chapters."""
    _get(session, book_id)
    queued = book_index.enqueue_book_index(services, book_id)
    return IndexRequested(book_id=book_id, chapters=queued)


@router.put("/books/{book_id}/license")
def change_license(
    book_id: str,
    change: LicenseChange,
    session: SessionDep,
    services: ServicesDep,
    principal: AdminDep,
) -> BookView:
    """Change the license class of the book, its chapters and what derives from them. Making a book
    "open" asserts it can be redistributed: it requires `confirm`."""
    try:
        chosen = LicenseClass(change.license_class)
    except ValueError as exc:
        raise LibraryError('The class is "open" or "private".') from exc
    row = _get(session, book_id)
    if chosen is LicenseClass.OPEN and not change.confirm:
        raise LibraryError(
            'Making a book "open" asserts you may redistribute it (it enters the public '
            "releases): repeat with confirm=true if you have that right."
        )
    try:
        licenses.set_source_class(session, f"book:{book_id}", chosen)
    except licenses.OverrideKeepsPrivate as exc:
        raise LibraryError(str(exc)) from exc
    session.commit()
    services.registry.sync(session)  # a fonte aberta em memória fica com a classe nova
    session.refresh(row)
    return _view(session, services, row)


__all__ = ["BookNotFound", "router"]
