"""Entrada de um livro na biblioteca (`ariadne books add`).

A ordem importa:

1. **DRM primeiro.** O ficheiro cifrado nunca chega ao Calibre nem ao índice (`check_drm`).
2. A classe de licença é obrigatória (`open` ou `private`): não há valor por omissão.
3. O Calibre guarda o ficheiro (e converte MOBI para EPUB); os metadados vêm dele, com o que o
   próprio ficheiro diz como alternativa.
4. A extração por capítulos confirma que o livro se lê e fica no índice (`sync_books`).

Se algo falha depois de o Calibre ter o livro, o livro sai do Calibre (não ficam órfãos).
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne.books.backend import LibraryBook, LibraryError
from ariadne.books.extract import BookError, check_isolated, file_sha256, slugify
from ariadne.books.index import sync_books
from ariadne.books.source import extracted
from ariadne.db import models
from ariadne.services import Services
from ariadne.sources.base import SourceError
from ariadne.sources.license import LicenseClass
from ariadne.sources.zim import LANGUAGE_2

logger = logging.getLogger(__name__)

SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


class AddError(Exception):
    """O livro não pôde entrar; a mensagem diz porquê e o que fazer."""


@dataclass(frozen=True)
class AddResult:
    book_id: str
    created: bool  # False: já estava na biblioteca (mesmo ficheiro)
    title: str
    format: str
    chapters: int
    note: str
    calibre_id: int | None
    license_class: str
    held: int = 0


def _unique_id(session: Session, wanted: str) -> str:
    candidate, n = wanted, 1
    while session.get(models.Book, candidate) is not None:
        n += 1
        candidate = f"{wanted[: 78 - len(str(n))]}-{n}"
    return candidate


def add_book(
    services: Services,
    file: Path,
    *,
    license_class: LicenseClass,
    license_name: str,
    attribution: str = "",
    book_id: str | None = None,
    title: str | None = None,
    authors: Sequence[str] = (),
) -> AddResult:
    """Adiciona o livro `file` (EPUB, PDF ou MOBI) à biblioteca e ao índice. O título e os autores
    dados mandam nos do ficheiro (um PDF costuma vir sem metadados)."""
    settings = services.settings
    if not isinstance(license_class, LicenseClass):
        raise AddError("The license class is required: 'open' or 'private'.")
    if not license_name.strip():
        raise AddError(
            'State the book\'s license (e.g. "Public domain", "CC BY-SA 4.0", "Purchased").'
        )
    if book_id is not None and not SLUG.match(book_id):
        raise AddError(
            f"Invalid id {book_id!r}: lowercase letters, digits, '.', '_' and '-' (up to 80)."
        )
    if not file.is_file():
        raise AddError(f"File not found: {file}")
    size = file.stat().st_size
    if size > settings.books_max_bytes:
        raise AddError(
            f"File too large ({size} bytes; maximum {settings.books_max_bytes}, BOOKS_MAX_BYTES)."
        )
    try:
        fmt = check_isolated(file)  # formato e DRM, antes de o Calibre ou o índice o verem
    except BookError as exc:  # inclui DrmError
        raise AddError(str(exc)) from exc

    sha = file_sha256(file)
    with services.sessions() as session:
        existing = session.scalars(
            select(models.Book).where(models.Book.file_sha256 == sha)
        ).first()
        if existing is not None and existing.status == "active":
            return _result(existing, created=False)
        revive = existing.id if existing is not None else None  # um livro removido que volta

    library = services.get_library()
    try:
        added = library.add(file, title=title, authors=authors)
    except LibraryError as exc:
        raise AddError(str(exc)) from exc
    try:
        return _register(
            services,
            added,
            fmt,
            Entry(license_class, license_name.strip(), attribution.strip(), book_id, title),
            revive,
        )
    except BaseException:
        try:
            library.remove(added.id)  # nada de livros órfãos no Calibre
        except LibraryError:
            logger.warning("Calibre: could not remove book %d after the failure", added.id)
        raise


@dataclass(frozen=True)
class Entry:
    """O que quem adiciona o livro declara."""

    license_class: LicenseClass
    license_name: str
    attribution: str
    book_id: str | None
    title: str | None


def _register(
    services: Services, added: LibraryBook, fmt: str, entry: Entry, revive: str | None
) -> AddResult:
    settings = services.settings
    library = services.get_library()
    extraction_format = "EPUB" if fmt == "mobi" else fmt.upper()
    try:
        if extraction_format not in added.formats:
            added = library.convert(added.id, extraction_format)  # MOBI -> EPUB, no Calibre
    except LibraryError as exc:
        raise AddError(str(exc)) from exc
    relative = added.path_of(extraction_format)
    if relative is None:
        raise AddError(f"Calibre did not keep the {extraction_format} format.")
    file = settings.books_dir / relative
    if not file.is_file():
        raise AddError(
            f"The file {relative} is not visible in BOOKS_DIR ({settings.books_dir}): "
            "the Calibre library volume must be mounted in the server/worker."
        )
    sha = file_sha256(file)
    try:  # também o que a conversão produziu (DRM incluído); fica na cache da fonte
        content = extracted(file, sha)
    except (BookError, SourceError) as exc:
        raise AddError(str(exc)) from exc
    meta = content.meta
    chosen_title = (entry.title or added.title or meta.title or file.stem).strip()
    values: dict[str, Any] = {
        "title": chosen_title,
        "authors": list(added.authors or meta.authors),
        "language": _language(added.language or meta.language),
        "publisher": added.publisher or meta.publisher,
        "published": added.published or meta.published,
        "description": added.description or meta.description,
        "identifiers": {**meta.identifiers, **added.identifiers},
        "format": extraction_format.lower(),
        "file_path": relative,
        "file_sha256": sha,
        "file_size": file.stat().st_size,
        "calibre_id": added.id,
        "license_class": entry.license_class.value,
        "license_name": entry.license_name,
        "attribution": entry.attribution,
        "chapters": len(content.chapters),
        "pages": content.pages,
        "extraction_note": content.note,
        "status": "active",
    }
    with services.sessions() as session:
        duplicate = session.scalars(
            select(models.Book).where(models.Book.file_sha256 == sha)
        ).first()
        if duplicate is not None and duplicate.status == "active":
            library.remove(added.id)  # a conversão deu um ficheiro que já cá estava
            return _result(duplicate, created=False)
        revive = duplicate.id if duplicate is not None else revive
        if revive is not None:
            # O livro tinha sido removido: volta com o mesmo id, os dados novos e a extração por
            # refazer (os capítulos e os documentos antigos são os mesmos e atualizam-se).
            row = session.get(models.Book, revive)
            assert row is not None
            for key, value in values.items():
                setattr(row, key, value)
            row.extractor_version = 0
            row.indexed_at = None
            row.kavita_library_id = row.kavita_series_id = row.kavita_chapter_id = None
            final_id = revive
        else:
            wanted = entry.book_id or slugify(chosen_title, 60) or f"livro-{added.id}"
            final_id = entry.book_id or _unique_id(session, wanted)
            if entry.book_id and session.get(models.Book, entry.book_id) is not None:
                raise AddError(f"A book with id {entry.book_id!r} already exists.")
            session.add(models.Book(id=final_id, **values))
        session.commit()
    try:
        report = sync_books(services, book_ids=[final_id])
        if report.errors:
            raise AddError("; ".join(report.errors))
    except BaseException:
        # A extração/indexação falhou depois de o livro estar na tabela: sem isto ficava `active`,
        # a apontar para um ficheiro que quem chama vai tirar do Calibre, e o worker repetia para
        # sempre. Fica `removed` (sai da pesquisa); repetir `books add` revive-o e repara-o.
        with services.sessions() as session:
            orphan = session.get(models.Book, final_id)
            if orphan is not None:
                orphan.status = "removed"
                session.commit()
            services.registry.sync(session)
        raise
    with services.sessions() as session:
        row = session.get(models.Book, final_id)
        assert row is not None
        return _result(row, created=True, held=report.held)


def _language(code: str | None) -> str | None:
    if not code:
        return None
    key = code.strip().lower().split("-")[0]
    return LANGUAGE_2.get(key, key) or None


def _result(row: models.Book, *, created: bool, held: int = 0) -> AddResult:
    return AddResult(
        book_id=row.id,
        created=created,
        title=row.title,
        format=row.format,
        chapters=row.chapters,
        note=row.extraction_note or "",
        calibre_id=row.calibre_id,
        license_class=row.license_class,
        held=held,
    )
