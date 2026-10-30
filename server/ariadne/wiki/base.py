"""Interface `WikiBackend` (ARCHITECTURE.md, secção 14.9): o núcleo só conhece isto."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class WikiError(Exception):
    """O motor da wiki recusou ou falhou um pedido."""


class WikiNotFound(WikiError):
    """A página não existe."""


class WikiUnavailable(WikiError):
    """Erro de rede ou o motor está em baixo: tenta-se mais tarde."""


@dataclass(frozen=True)
class WikiHit:
    page_id: int
    path: str
    title: str
    description: str


@dataclass(frozen=True)
class WikiPage:
    page_id: int
    path: str
    title: str
    content: str
    description: str


class WikiBackend(ABC):
    @abstractmethod
    def upsert_page(
        self,
        path: str,
        title: str,
        content_md: str,
        description: str,
        tags: list[str],
        *,
        locale: str | None = None,
    ) -> int:
        """Cria a página no caminho (sem o locale à frente: o Wiki.js põe-no) e no locale dado
        (por omissão o do site), ou atualiza-a se já existe (o motor guarda a revisão anterior).
        Devolve o id da página."""

    @abstractmethod
    def move_page(self, page_id: int, path: str, locale: str) -> None:
        """Move a página para `path` no `locale` (o motor guarda o histórico). O destino não pode
        existir. `WikiNotFound` se a página já não existe."""

    def active_locales(self) -> frozenset[str] | None:
        """Os locales em que o motor aceita páginas, ou `None` se não se sabe (o chamador usa
        `WIKIJS_LOCALES`)."""
        return None

    @abstractmethod
    def get_page(self, page_id: int) -> WikiPage | None:
        """A página, ou `None` se já não existe."""

    @abstractmethod
    def search(self, query: str, k: int, locale: str | None = None) -> list[WikiHit]:
        """Pesquisa de texto nas páginas (título e descrição, e o que o motor indexar)."""

    @abstractmethod
    def upload_asset(self, folder: str, filename: str, data: bytes, mimetype: str) -> str:
        """Envia um ficheiro (imagem) para a pasta de assets (`ariadne/ab`, criada se não existe)
        e devolve o URL público relativo (`/ariadne/ab/<nome>`). Enviar o mesmo nome outra vez
        substitui o ficheiro."""

    @abstractmethod
    def asset_exists(self, path: str) -> bool:
        """Se o asset `pasta/nome` (sem `/` à frente) já está no motor."""

    @abstractmethod
    def health(self) -> str:
        """`ok` ou o motivo de não responder (informativo, para o `/health`)."""

    @abstractmethod
    def close(self) -> None:
        """Liberta os recursos."""
