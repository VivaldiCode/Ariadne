"""A interface `LibraryBackend` (STRUCTURE.md, 6.3): a biblioteca de livros canónica.

Como o `WikiBackend` e o `CourseBackend`: o núcleo só conhece isto e o Calibre é a primeira
implementação (`ariadne.books.calibre`). Adicionar um livro, ler os metadados e converter entre
formatos fazem-se aqui; a leitura do texto (capítulos) é do Ariadne (`ariadne.books.extract`) e
a leitura pelas pessoas é do Kavita.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


class LibraryError(Exception):
    """A biblioteca recusou ou falhou."""


class LibraryUnavailable(LibraryError):
    """A biblioteca não responde (em baixo, sem configuração, chave errada)."""


@dataclass(frozen=True)
class LibraryBook:
    """Um livro na biblioteca, como o gestor o descreve."""

    id: int  # o id no gestor (Calibre)
    title: str
    authors: tuple[str, ...] = ()
    language: str | None = None
    publisher: str | None = None
    published: str | None = None
    description: str = ""
    identifiers: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    # Formato (maiúsculas: EPUB, PDF, MOBI) -> caminho do ficheiro relativo à biblioteca.
    formats: dict[str, str] = field(default_factory=dict)

    def path_of(self, fmt: str) -> str | None:
        return self.formats.get(fmt.upper())


class LibraryBackend(ABC):
    name: ClassVar[str]

    @abstractmethod
    def check(self) -> dict[str, str]:
        """Confirma que a biblioteca responde; devolve versões. `LibraryUnavailable` se não."""

    @abstractmethod
    def add(
        self, file: Path, *, title: str | None = None, authors: Sequence[str] = ()
    ) -> LibraryBook:
        """Adiciona o ficheiro (EPUB, PDF, MOBI) à biblioteca e devolve o livro. O título e os
        autores, se dados, mandam nos do ficheiro (um PDF costuma vir sem metadados)."""

    @abstractmethod
    def get(self, book_id: int) -> LibraryBook:
        """Um livro; `LibraryError` se não existe."""

    @abstractmethod
    def list_books(self) -> list[LibraryBook]:
        """Todos os livros."""

    @abstractmethod
    def convert(self, book_id: int, to: str) -> LibraryBook:
        """Acrescenta ao livro o formato `to` (ex.: EPUB a partir de MOBI). Idempotente."""

    @abstractmethod
    def remove(self, book_id: int) -> None:
        """Remove o livro da biblioteca (apaga os ficheiros)."""

    @abstractmethod
    def close(self) -> None:
        """Liberta os recursos."""
