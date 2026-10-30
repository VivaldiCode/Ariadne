"""A biblioteca de livros de brincar: guarda os ficheiros numa pasta, como o Calibre faria."""

import shutil
from collections.abc import Sequence
from pathlib import Path

from ariadne.books.backend import LibraryBackend, LibraryBook, LibraryError
from ariadne.books.extract import BookError, extract_book, sniff_format


class FakeLibrary(LibraryBackend):
    """Copia o ficheiro para `root/<n>/livro.<ext>` (o caminho relativo que o Calibre devolveria).
    `epub_for_mobi` é o EPUB que a «conversão» de um MOBI produz."""

    name = "fake"

    def __init__(self, root: Path, epub_for_mobi: Path | None = None) -> None:
        self.root = root
        self.epub_for_mobi = epub_for_mobi
        self.books: dict[int, LibraryBook] = {}
        self.added: list[Path] = []
        self.requested: list[tuple[str | None, tuple[str, ...]]] = []  # (título, autores) pedidos
        self.removed: list[int] = []
        self.fail_add: Exception | None = None

    def check(self) -> dict[str, str]:
        return {"fake": "1"}

    def add(
        self, file: Path, *, title: str | None = None, authors: Sequence[str] = ()
    ) -> LibraryBook:
        if self.fail_add is not None:
            raise self.fail_add
        self.added.append(file)
        self.requested.append((title, tuple(authors)))
        number = len(self.books) + 1
        fmt = sniff_format(file)
        folder = self.root / f"livro-{number}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(file, folder / f"livro.{fmt}")
        found_title, found_authors, language = file.stem, (), None
        if fmt in ("epub", "pdf"):
            try:
                meta = extract_book(file).meta
                found_title = meta.title or file.stem
                found_authors, language = meta.authors, meta.language
            except BookError:
                pass  # o Calibre aceita o que não se consegue ler por capítulos
        book = LibraryBook(
            id=number,
            title=title or found_title,
            authors=tuple(authors) or found_authors,
            language={"pt": "por", "en": "eng"}.get(language or "", language),
            formats={fmt.upper(): f"livro-{number}/livro.{fmt}"},
        )
        self.books[number] = book
        return book

    def get(self, book_id: int) -> LibraryBook:
        if book_id not in self.books:
            raise LibraryError(f"livro {book_id} não existe")
        return self.books[book_id]

    def list_books(self) -> list[LibraryBook]:
        return list(self.books.values())

    def convert(self, book_id: int, to: str) -> LibraryBook:
        book = self.get(book_id)
        if self.epub_for_mobi is None:
            raise LibraryError("sem conversão")
        folder = self.root / f"livro-{book_id}"
        shutil.copy(self.epub_for_mobi, folder / f"livro.{to.lower()}")
        converted = LibraryBook(
            id=book.id,
            title=book.title,
            authors=book.authors,
            language=book.language,
            formats={**book.formats, to.upper(): f"livro-{book_id}/livro.{to.lower()}"},
        )
        self.books[book_id] = converted
        return converted

    def remove(self, book_id: int) -> None:
        self.removed.append(book_id)
        self.books.pop(book_id, None)
        shutil.rmtree(self.root / f"livro-{book_id}", ignore_errors=True)

    def close(self) -> None:
        pass
