"""Interface `Source`: o núcleo só conhece isto; o ZIM é a primeira implementação."""

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ariadne.sources.classify import ContentClass, effective_class
from ariadne.sources.license import LicenseClass, parse_license_class


class SourceError(Exception):
    """Falha ao ler uma fonte."""


class NotFound(SourceError):
    """O caminho não existe na fonte."""


class NotAnArticle(SourceError):
    """O caminho existe mas não é texto legível (imagem, CSS, PDF…). Leva o que se sabe do item
    (`path` canónico, `mimetype`, `title`) para quem o quiser tratar (um PDF é um livro)."""

    def __init__(
        self, message: str = "", *, path: str = "", mimetype: str = "", title: str = ""
    ) -> None:
        super().__init__(message)
        self.path, self.mimetype, self.title = path, mimetype, title


@dataclass(frozen=True)
class Section:
    """Secção de um artigo: um título h2/h3 e os blocos de texto até ao seguinte do mesmo nível."""

    title: str
    level: int  # 2 ou 3
    start: int  # índice do primeiro bloco (o próprio título)
    end: int  # índice do bloco seguinte ao último da secção


@dataclass(frozen=True)
class Block:
    """Um parágrafo, título ou item de lista do texto limpo."""

    kind: str  # 'p' | 'h' | 'li'
    text: str
    level: int = 0  # só nos títulos


@dataclass(frozen=True)
class Link:
    """Ligação interna de um artigo para outro da mesma fonte (caminho já canónico)."""

    path: str
    title: str
    block: int  # índice do bloco onde aparece


@dataclass(frozen=True)
class Article:
    """Artigo lido de uma fonte, com o caminho canónico (redirects já seguidos)."""

    path: str
    title: str
    html: str
    blocks: tuple[Block, ...]
    sections: tuple[Section, ...]
    links: tuple[Link, ...]
    mimetype: str = "text/html"
    redirected_from: str | None = None
    redirected_title: str | None = None  # o título do redirect pedido: um título alternativo

    @property
    def text(self) -> str:
        return render_blocks(self.blocks)

    def section_text(self, section: Section) -> str:
        return render_blocks(self.blocks[section.start : section.end])

    def section_links(self, section: Section) -> list[Link]:
        return [link for link in self.links if section.start <= link.block < section.end]


def render_blocks(blocks: tuple[Block, ...]) -> str:
    """Junta os blocos em texto: itens de lista seguidos ficam em linhas consecutivas."""
    parts: list[str] = []
    previous: Block | None = None
    for block in blocks:
        if block.kind == "h":
            line = f"{'#' * block.level} {block.text}"
        elif block.kind == "li":
            line = f"- {block.text}"
        else:
            line = block.text
        if previous is not None:
            parts.append("\n" if previous.kind == "li" and block.kind == "li" else "\n\n")
        parts.append(line)
        previous = block
    return "".join(parts)


class Source(ABC):
    """Uma fonte de artigos lidos por caminho. O `id` é estável entre versões da fonte."""

    kind: ClassVar[str]
    # O que os documentos desta fonte são (`document.content_type`): artigo, lição ou capítulo.
    content_type: ClassVar[str] = "article"

    def __init__(
        self,
        *,
        id: str,
        title: str,
        language: str | None,
        version: str,
        wing: str,
        path: str | None = None,
        meta: dict[str, Any] | None = None,
        license_class: str | LicenseClass | None = None,
    ) -> None:
        if license_class is None:
            # Sem classe não há fonte (STRUCTURE.md, secção 7): nunca se assume «aberto».
            raise ValueError(f"source {id!r} does not declare a license class (open | private)")
        self.id = id  # ex.: 'zim:wikipedia_pt_all_nopic'
        self.title = title
        self.language = language  # ISO 639, a 2 letras quando há mapeamento simples
        self.version = version  # ex.: '2026-09'
        self.wing = wing  # ala (MemPalace)
        self.path = path  # onde vive (ficheiro ZIM, …)
        self.meta: dict[str, Any] = meta or {}
        # A classe de licença da fonte (secção 7): os documentos herdam-na e as saídas verificam-na.
        self.license_class: LicenseClass = parse_license_class(license_class)
        # A que a fonte declara; a efetiva (`license_class`) é a mais restritiva entre esta e a
        # marca persistente `source.license_override` (o registo aplica-a a cada sincronização).
        self.declared_license_class: LicenseClass = self.license_class
        # Correções manuais da classe por secção (`SOURCE_CLASS_OVERRIDES`); o registo preenche-o.
        self.class_overrides: Mapping[str, ContentClass] = {}

    def content_class_of(self, path: str | None = None) -> ContentClass:
        """A classe da fonte, ou da secção a que o caminho pertence (o prefixo mais longo ganha)."""
        default = ContentClass(str(self.meta.get("content_class", "article")))
        return effective_class(self.id, default, self.class_overrides, path)

    def archive_only_for(self, path: str | None = None) -> bool:
        """O caminho só serve de arquivo: é de um curso com extrator, que se lê pelas lições."""
        return self.content_class_of(path) is ContentClass.COURSE and bool(
            self.meta.get("extractable")
        )

    @abstractmethod
    def read(self, path: str) -> Article:
        """Lê o artigo (segue redirects). `NotFound` ou `NotAnArticle` se não houver texto."""

    @abstractmethod
    def internal_links(self, path: str) -> list[str]:
        """Caminhos canónicos dos artigos da mesma fonte ligados a partir de `path`."""

    def resolve_link(self, current: str, href: str) -> tuple[str, str] | None:
        """(caminho canónico, título) do artigo da mesma fonte para onde aponta o `href` do
        artigo `current`; `None` se não aponta para um artigo (âncora, URL externo, imagem…)."""
        return None

    @abstractmethod
    def iter_paths(self) -> Iterator[str]:
        """Caminhos canónicos de todos os artigos (sem redirects)."""

    def redirects(self) -> Iterator[tuple[str, str]]:
        """(título, caminho canónico do artigo) de cada redirect: os títulos alternativos do
        artigo. Por omissão a fonte não tem redirects."""
        return iter(())

    def find_by_title(self, title: str) -> tuple[str, str] | None:
        """(caminho canónico, título) do artigo com este título (redirects seguidos), sem ler o
        conteúdo; `None` se a fonte não o tem (ou não sabe procurar por título)."""
        return None

    @abstractmethod
    def article_count(self) -> int:
        """Número de artigos, tal como a fonte o declara (barato, sem percorrer tudo)."""

    @abstractmethod
    def close(self) -> None:
        """Liberta os recursos da fonte."""
