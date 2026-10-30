"""`BookSource`: um livro da biblioteca (`book:<id>`) como fonte, capítulo a capítulo.

Com isto um capítulo é um documento como outro qualquer: o indexador parte-o em chunks, o linker
liga-o aos artigos e às lições por título e por vetor, `read` devolve-o e `neighbors` mostra o que
lhe está ligado. O caminho de um capítulo é `NNN-titulo` (ver `extract.chapter_path`); a citação é
`book:<id>/<capítulo>#<posição>`, onde a posição é o deslocamento (em caracteres) no texto que
`read` devolve para o capítulo — estável enquanto o ficheiro e o `EXTRACTOR_VERSION` não mudam.

O ficheiro lê-se da biblioteca do Calibre (`BOOKS_DIR`, montada só de leitura no server e nos
workers) e extrai-se por inteiro uma vez; as últimas extrações ficam em memória (`_EXTRACTED`).
"""

import html as htmllib
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ariadne.books.extract import (
    EXTRACTOR_VERSION,
    BookError,
    ExtractedBook,
    ExtractedChapter,
    extract_book_isolated,
    extractor_fingerprint,
)
from ariadne.sources.base import Article, NotFound, Source, SourceError
from ariadne.sources.license import LicenseClass
from ariadne.sources.zim import extract_blocks, make_sections

_CACHE = 64  # capítulos em memória por livro
_EXTRACTED_BOOKS = 4  # livros extraídos em memória, no processo
_EXTRACTED: OrderedDict[tuple[str, str, str], ExtractedBook] = OrderedDict()
_EXTRACTED_LOCK = threading.Lock()


def source_id_for(book_id: str) -> str:
    return f"book:{book_id}"


@dataclass(frozen=True)
class BookData:
    """O que a fonte precisa de um livro (cópia do que está em `book`, sem a sessão)."""

    id: str
    title: str
    file: Path  # caminho completo do ficheiro extraído
    format: str
    file_sha256: str
    license_class: LicenseClass
    license_name: str = ""
    attribution: str = ""
    authors: tuple[str, ...] = ()
    language: str | None = None
    chapters: int = 0
    pages: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def extracted(file: Path, sha256: str) -> ExtractedBook:
    """O livro extraído (com cache limitada), num subprocesso com limites de memória e de tempo. A
    chave inclui a impressão digital do extrator (versão, bs4, pypdf, saneamento do HTML)."""
    key = (str(file), sha256, extractor_fingerprint())
    with _EXTRACTED_LOCK:
        hit = _EXTRACTED.get(key)
        if hit is not None:
            _EXTRACTED.move_to_end(key)
            return hit
    if not file.is_file():
        raise SourceError(f"the book file is not accessible: {file}")
    try:
        book = extract_book_isolated(file)
    except BookError as exc:
        raise SourceError(str(exc)) from exc
    with _EXTRACTED_LOCK:
        _EXTRACTED[key] = book
        while len(_EXTRACTED) > _EXTRACTED_BOOKS:
            _EXTRACTED.popitem(last=False)
    return book


class BookSource(Source):
    kind = "book"
    content_type = "chapter"

    def __init__(self, book: BookData) -> None:
        super().__init__(
            id=source_id_for(book.id),
            title=book.title,
            language=book.language,
            version=book.file_sha256[:12],
            wing=f"book-{book.id}",
            license_class=book.license_class,
            path=str(book.file),
            meta={
                "content_class": "book",
                "archive_only": False,
                "book_id": book.id,
                "book_format": book.format,
                "authors": list(book.authors),
                "license_name": book.license_name,
                "attribution": book.attribution,
                "chapters": book.chapters,
                "pages": book.pages,
                "file_sha256": book.file_sha256,
                "extractor_version": EXTRACTOR_VERSION,
                "extractor_hash": extractor_fingerprint(),
                **book.extra,
            },
        )
        self.book = book
        self._cache: OrderedDict[str, Article] = OrderedDict()
        self._lock = threading.Lock()

    # --- o livro extraído ---

    def content(self) -> ExtractedBook:
        return extracted(self.book.file, self.book.file_sha256)

    def chapter(self, path: str) -> tuple[int, ExtractedChapter]:
        """(posição a contar de 1, capítulo); `NotFound` se o caminho não existe."""
        for position, chapter in enumerate(self.content().chapters, start=1):
            if chapter.path == path:
                return position, chapter
        raise NotFound(path)

    # --- leitura ---

    def read(self, path: str) -> Article:
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None:
                self._cache.move_to_end(path)
                return cached
        article = self._build(path)
        with self._lock:
            self._cache[path] = article
            while len(self._cache) > _CACHE:
                self._cache.popitem(last=False)
        return article

    def _build(self, path: str) -> Article:
        _, chapter = self.chapter(path)
        title = htmllib.escape(chapter.title)
        html = (
            f"<html><head><title>{title}</title></head>"
            f"<body><main><h1>{title}</h1>{chapter.html}</main></body></html>"
        )
        blocks, _ = extract_blocks(html, lambda href: None)
        return Article(
            path=path,
            title=chapter.title,
            html=html,
            blocks=tuple(blocks),
            sections=tuple(make_sections(chapter.title, blocks)),
            links=(),
        )

    def internal_links(self, path: str) -> list[str]:
        return []

    def iter_paths(self) -> Iterator[str]:
        for chapter in self.content().chapters:
            yield chapter.path

    def find_by_title(self, title: str) -> tuple[str, str] | None:
        wanted = title.strip().casefold()
        for chapter in self.content().chapters:
            if chapter.title.casefold() == wanted:
                return chapter.path, chapter.title
        return None

    def article_count(self) -> int:
        # O que o índice guardou; sem abrir o ficheiro (o catálogo pergunta isto a cada pedido).
        return self.book.chapters or len(self.content().chapters)

    def close(self) -> None:
        with self._lock:
            self._cache.clear()
