"""Fonte ZIM: leitura direta com python-libzim (ARCHITECTURE.md, secção 4.3).

O HTML é limpo com BeautifulSoup + `html.parser` (puro Python, sem dependências nativas: o
servidor de produção não tem AVX).
"""

import os
import posixpath
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from libzim.reader import Archive, Item

from ariadne.sources.base import (
    Article,
    Block,
    Link,
    NotAnArticle,
    NotFound,
    Section,
    Source,
    SourceError,
)
from ariadne.sources.classify import ContentClass, classify_zim
from ariadne.sources.license import LicenseClass
from ariadne.sources.projects import identify

# O ZIM usa ISO 639-3 (3 letras). Guardamos as 2 letras quando o mapeamento é simples.
LANGUAGE_2 = {
    "ara": "ar", "bul": "bg", "cat": "ca", "ces": "cs", "dan": "da", "deu": "de", "ell": "el",
    "eng": "en", "est": "et", "eus": "eu", "fas": "fa", "fin": "fi", "fra": "fr", "glg": "gl",
    "heb": "he", "hin": "hi", "hrv": "hr", "hun": "hu", "ind": "id", "ita": "it", "jpn": "ja",
    "kor": "ko", "lat": "la", "lit": "lt", "lav": "lv", "nld": "nl", "nor": "no", "pol": "pl",
    "por": "pt", "ron": "ro", "rus": "ru", "slk": "sk", "slv": "sl", "spa": "es", "srp": "sr",
    "swe": "sv", "tur": "tr", "ukr": "uk", "vie": "vi", "zho": "zh",
}  # fmt: skip


def _words(text: str) -> list[str]:
    return text.split()


# Artigos já limpos que a fonte guarda em memória (o mesmo artigo é lido várias vezes por secção).
_CACHE_SIZE = 32
# Fim do nome do ficheiro Kiwix: ..._2026-08
_FILE_DATE = re.compile(r"_(\d{4}-\d{2})$")
_SPACES = re.compile(r"\s+")

# O índice do currículo do freeCodeCamp: é o que o extrator de cursos sabe ler.
CURRICULUM_INDEX = "content/curriculum/index.json"

# Elementos sem texto útil para um leitor.
_DROP_TAGS = _words(
    "script style noscript template svg head link meta nav footer aside form button select "
    "iframe audio video canvas object input textarea dialog"
)
# Ruído típico da Wikipédia (mwoffliner): navegação, avisos, referências, "editar".
_DROP_SELECTORS = ", ".join(
    [
        ".navbox",
        ".vertical-navbox",
        ".sidebar",
        ".metadata",
        ".ambox",
        ".mw-editsection",
        ".noprint",
        ".mw-empty-elt",
        ".mw-cite-backlink",
        ".reflist",
        "ol.references",
        ".printfooter",
        ".catlinks",
        ".toc",
        "#toc",
        ".sistersitebox",
        "sup.reference",
        "[role=navigation]",
    ]
)
# Elementos que separam blocos de texto.
_BLOCK_TAGS = frozenset(
    _words(
        "address article blockquote body caption dd details div dl dt fieldset figcaption figure "
        "hr li main ol p pre section summary table tbody tfoot thead tr ul center"
    )
)
_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_NON_FILE_SCHEMES = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


_INVISIBLE = {ord(c): None for c in "\u200b\u200c\u200d\ufeff\u00ad"}  # espaços de largura zero, …


def clean_spaces(text: str) -> str:
    return _SPACES.sub(" ", text.translate(_INVISIBLE)).strip()


def _decode(metadata: bytes) -> str:
    return bytes(metadata).decode("utf-8", errors="replace").strip()


def _iso_date(raw: str) -> str | None:
    """'2026-08-02' -> '2026-08'."""
    match = re.match(r"(\d{4})-(\d{2})", raw)
    return f"{match.group(1)}-{match.group(2)}" if match else None


# Os ZIM abertos ficam num conjunto limitado e partilhado: uma pasta com centenas de ZIM esgotaria
# os descritores de ficheiro do processo (o Archive do libzim guarda o ficheiro aberto). Um ZIM fora
# do conjunto volta a abrir-se na próxima leitura; fecha quando o último Archive é libertado.
_MAX_OPEN = max(1, int(os.environ.get("ZIM_MAX_OPEN", "64")))
_open_archives: OrderedDict[str, Archive] = OrderedDict()
_open_lock = threading.Lock()


def _pooled(path: Path, archive: Archive | None = None) -> Archive:
    key = str(path)
    with _open_lock:
        cached = _open_archives.get(key)
        if cached is not None:
            _open_archives.move_to_end(key)
            return cached
    if archive is None:
        archive = Archive(path)  # fora do lock: abrir um ZIM grande demora
    with _open_lock:
        _open_archives[key] = archive
        _open_archives.move_to_end(key)
        while len(_open_archives) > _MAX_OPEN:
            _open_archives.popitem(last=False)
    return archive


def _forget(path: Path) -> None:
    with _open_lock:
        _open_archives.pop(str(path), None)


@dataclass(frozen=True)
class EntryInfo:
    """O que se sabe de uma entrada do ZIM sem ler o conteúdo."""

    path: str  # canónico (os redirects já seguidos)
    mimetype: str
    title: str
    size: int  # bytes


class ZimSource(Source):
    kind = "zim"

    def __init__(self, archive: Archive, path: Path) -> None:
        self._zim_path = path
        _pooled(path, archive)
        self._cache: OrderedDict[str, Article] = OrderedDict()
        self._cache_lock = threading.Lock()
        names = set(archive.metadata_keys)

        def meta(key: str) -> str:
            return _decode(archive.get_metadata(key)) if key in names else ""

        stem = path.stem  # é o nome do livro no kiwix-serve: /content/<stem>/…
        name = meta("Name") or _FILE_DATE.sub("", stem)
        name = name.replace("/", "_")  # o '/' separa a fonte do caminho no document_id
        # O Name não distingue variantes (maxi/nopic/mini): o flavour entra no id, para que duas
        # variantes na mesma pasta sejam duas fontes e não uma a apagar a outra.
        flavour = meta("Flavour").replace("/", "_")
        source_name = name if not flavour or name.endswith(f"_{flavour}") else f"{name}_{flavour}"
        raw_language = meta("Language")
        first_language = raw_language.split(",")[0].strip()
        version = (
            _iso_date(meta("Date"))
            or (m.group(1) if (m := _FILE_DATE.search(stem)) else None)
            or datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m")
        )
        language = LANGUAGE_2.get(first_language, first_language) or None
        extractable = self._has_entry(CURRICULUM_INDEX)
        classification = classify_zim(
            name=name,
            creator=meta("Creator"),
            tags=meta("Tags"),
            description=meta("Description"),
        )
        super().__init__(
            id=f"zim:{source_name}",
            license_class=LicenseClass.OPEN,  # STRUCTURE.md, secção 7: os ZIM são abertos
            title=meta("Title") or name,
            language=language,
            version=version,
            wing=name,
            path=str(path),
            meta={
                "kiwix_book": stem,
                "zim_name": name,
                "zim_language": raw_language,
                "flavour": meta("Flavour"),
                "date": meta("Date"),
                "creator": meta("Creator"),
                "description": meta("Description"),
                "uuid": str(archive.uuid),
                "articles": archive.article_count,
                **identify(name, language),  # project, wiki_language e site (Wikidata)
                # Classificação à entrada (STRUCTURE.md, secção 4): artigo, curso ou livro.
                "content_class": classification.content_class.value,
                "class_reason": classification.reason,
                # Com extrator (hoje só o freeCodeCamp) a fonte é só arquivo; sem ele, pesquisa-se.
                "extractable": extractable,
                "archive_only": (
                    classification.content_class is ContentClass.COURSE and extractable
                ),
            },
        )

    def _has_entry(self, path: str) -> bool:
        try:
            self._archive.get_entry_by_path(path)
        except (KeyError, RuntimeError):
            return False
        return True

    @property
    def _archive(self) -> Archive:
        return _pooled(self._zim_path)

    @classmethod
    def open(cls, path: Path) -> "ZimSource":
        # Qualquer erro do libzim (há ZIM reais cujo erro nem é UTF-8 válido: UnicodeDecodeError)
        # fica neste ZIM: uma biblioteca com centenas de ficheiros não pode parar por um.
        try:
            return cls(Archive(path), path)
        except Exception as exc:
            raise SourceError(f"Unreadable ZIM: {path} ({type(exc).__name__}: {exc})") from exc

    # --- leitura ---------------------------------------------------------------------------

    def _item(self, path: str) -> Item:
        """O item do caminho, com os redirects seguidos."""
        try:
            return self._archive.get_entry_by_path(path).get_item()
        except (KeyError, RuntimeError) as exc:  # RuntimeError: redirect partido
            raise NotFound(path) from exc

    def read(self, path: str) -> Article:
        item = self._item(path)
        if not item.mimetype.startswith("text/html"):
            raise NotAnArticle(
                f"{path} ({item.mimetype})",
                path=item.path,
                mimetype=item.mimetype,
                title=item.title or "",
            )
        article = self._cached(item)
        if item.path == path:
            return article
        title = self._archive.get_entry_by_path(path).title  # o do redirect, não o do artigo
        return replace(article, redirected_from=path, redirected_title=title or None)

    def internal_links(self, path: str) -> list[str]:
        article = self.read(path)
        return list(dict.fromkeys(link.path for link in article.links))

    def iter_paths(self) -> Iterator[str]:
        archive = self._archive  # um só acesso ao conjunto de ZIM abertos
        for index in range(archive.entry_count):
            entry = archive._get_entry_by_id(index)
            if entry.is_redirect:
                continue
            if entry.get_item().mimetype.startswith("text/html"):
                yield entry.path

    def redirects(self) -> Iterator[tuple[str, str]]:
        archive = self._archive  # um só acesso ao conjunto de ZIM abertos
        for index in range(archive.entry_count):
            entry = archive._get_entry_by_id(index)
            if not entry.is_redirect:
                continue
            try:
                item = entry.get_item()
            except RuntimeError:  # redirect partido
                continue
            if item.mimetype.startswith("text/html") and entry.title:
                yield entry.title, item.path

    def random_paths(self, n: int) -> list[str]:
        """Até `n` caminhos de artigos HTML, sorteados pelo próprio ZIM (`get_random_entry`), sem
        o percorrer: custa `n` leituras mesmo numa Wikipédia de 2 milhões de entradas. Os
        redirects seguem-se, os que não são HTML saltam-se e não há repetidos. Um ZIM com poucos
        artigos devolve menos de `n` (o sorteio desiste após `n * 10` tentativas)."""
        found: dict[str, None] = {}
        archive = self._archive
        for _ in range(max(n, 0) * 10):
            if len(found) >= n:
                break
            try:
                item = archive.get_random_entry().get_item()
            except (KeyError, RuntimeError):  # ZIM sem artigos, ou um redirect partido
                continue
            if item.mimetype.startswith("text/html"):
                found[item.path] = None
        return list(found)

    def find_by_title(self, title: str) -> tuple[str, str] | None:
        """O artigo com este título, sem ler o conteúdo: primeiro pelo caminho canónico («Lisboa»,
        «Revolução_dos_Cravos»: é o nome que o Wikidata e os dumps usam), só depois pelo título de
        apresentação do índice de títulos do ZIM (que pode ser outro)."""
        underscored = title.replace(" ", "_")
        for lookup, name in (
            (self._archive.get_entry_by_path, underscored),
            (self._archive.get_entry_by_path, title),
            (
                self._archive.get_entry_by_path,
                f"A/{underscored}",
            ),  # ZIM antigo, com espaços de nomes
            (self._archive.get_entry_by_title, title),
        ):
            try:
                item = lookup(name).get_item()
            except (KeyError, RuntimeError):  # não existe, ou redirect partido
                continue
            if item.mimetype.startswith("text/html"):
                return item.path, item.title or item.path
        return None

    def article_count(self) -> int:
        return int(self._archive.article_count)

    # --- entradas em bruto (extratores de cursos) -------------------------------------------

    def entry_paths(self, prefix: str = "") -> Iterator[str]:
        """Caminhos de todas as entradas que não são redirects (qualquer tipo de conteúdo)."""
        archive = self._archive  # um só acesso ao conjunto de ZIM abertos
        for index in range(archive.entry_count):
            entry = archive._get_entry_by_id(index)
            if not entry.is_redirect and entry.path.startswith(prefix):
                yield entry.path

    def entry_infos(self, accept: Callable[[str, str], bool] | None = None) -> Iterator[EntryInfo]:
        """As entradas que não são redirects, com o tipo e o tamanho. `accept(mimetype, caminho)`
        escolhe as que interessam *antes* de ler o tamanho (que abre o cluster da entrada): num
        ZIM com milhões de HTML só se paga isso nas que passam."""
        archive = self._archive  # um só acesso ao conjunto de ZIM abertos
        for index in range(archive.entry_count):
            entry = archive._get_entry_by_id(index)
            if entry.is_redirect:
                continue
            item = entry.get_item()
            if accept is not None and not accept(item.mimetype, item.path):
                continue
            yield EntryInfo(item.path, item.mimetype, item.title or "", int(item.size))

    def entry_info(self, path: str) -> EntryInfo:
        """A entrada de `path` (segue redirects); `NotFound` se não existe."""
        item = self._item(path)
        return EntryInfo(item.path, item.mimetype, item.title or "", int(item.size))

    def zim_metadata(self, key: str) -> str:
        """Um metadado do ZIM (Creator, Publisher, Source…); vazio se não existe."""
        archive = self._archive
        if key not in set(archive.metadata_keys):
            return ""
        return _decode(archive.get_metadata(key))

    def read_bytes(self, path: str) -> tuple[str, bytes]:
        """(tipo MIME, conteúdo) de uma entrada, sem interpretar o HTML: para os extratores que
        leem ficheiros de dados (JSON, Markdown) dentro do ZIM."""
        item = self._item(path)
        return item.mimetype, bytes(item.content)

    def close(self) -> None:
        with self._cache_lock:
            self._cache.clear()
        _forget(self._zim_path)  # o Archive fecha quando o último que o usa o liberta

    # --- HTML -> texto ---------------------------------------------------------------------

    def _cached(self, item: Item) -> Article:
        with self._cache_lock:
            hit = self._cache.get(item.path)
            if hit is not None:
                self._cache.move_to_end(item.path)
                return hit
        article = self._parse(item)
        with self._cache_lock:
            self._cache[item.path] = article
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return article

    def _parse(self, item: Item) -> Article:
        html = bytes(item.content).decode("utf-8", errors="replace")
        blocks, links = extract_blocks(html, lambda href: self.resolve_link(item.path, href))
        title = item.title or item.path
        return Article(
            path=item.path,
            title=title,
            html=html,
            blocks=tuple(blocks),
            sections=tuple(make_sections(title, blocks)),
            links=tuple(links),
            mimetype=item.mimetype,
        )

    def resolve_link(self, current: str, href: str) -> tuple[str, str] | None:
        """(caminho canónico, título) de um href relativo ao artigo `current`; None se não serve."""
        if not href or href.startswith(("#", "//")) or _NON_FILE_SCHEMES.match(href):
            return None  # âncoras, externos, mailto:, javascript:, …
        target = unquote(urlsplit(href).path)
        if not target:
            return None
        if target.startswith("/"):
            target = target.lstrip("/")
        else:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(current), target))
        if target in (".", "..") or target.startswith("../"):
            return None
        try:
            item = self._archive.get_entry_by_path(target).get_item()
        except (KeyError, RuntimeError):
            return None
        if not item.mimetype.startswith("text/html") or item.path == current:
            return None
        return item.path, item.title or item.path


def make_sections(title: str, blocks: list[Block]) -> list[Section]:
    """Introdução (antes do 1.º título) mais uma secção por título h2/h3."""
    found: list[Section] = []
    first_heading = next((i for i, b in enumerate(blocks) if b.kind == "h"), len(blocks))
    if first_heading > 0:
        found.append(Section(title=title, level=1, start=0, end=first_heading))
    for index, block in enumerate(blocks):
        if block.kind != "h" or block.level not in (2, 3):
            continue
        end = next(
            (
                j
                for j in range(index + 1, len(blocks))
                if blocks[j].kind == "h" and blocks[j].level <= block.level
            ),
            len(blocks),
        )
        found.append(Section(title=block.text, level=block.level, start=index, end=end))
    return found


def content_root(html: str) -> BeautifulSoup | Tag:
    """A raiz do conteúdo do artigo, já sem o ruído (menus, avisos, referências, scripts) e com as
    fórmulas reduzidas ao TeX. Serve a extração de texto e a conversão para Markdown."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find(id="mw-content-text") or soup.find("main") or soup.find("article") or soup
    assert isinstance(root, Tag | BeautifulSoup)
    for tag in root.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in root.select(_DROP_SELECTORS):
        tag.decompose()
    for math in root.find_all("math"):  # fórmula: o TeX, não as duas versões renderizadas
        annotation = math.find("annotation")
        math.replace_with(NavigableString((annotation or math).get_text()))
    return root


def extract_blocks(
    html: str, resolve: Callable[[str], tuple[str, str] | None]
) -> tuple[list[Block], list[Link]]:
    root = content_root(html)
    extractor = _Extractor(resolve)
    # A caixa de informação sai do topo e entra depois da introdução (o leitor quer o texto).
    for infobox in root.select("table.infobox"):
        extractor.defer(infobox.extract())
    try:
        extractor.walk(root)
        extractor.emit_deferred()
    except RecursionError:  # HTML aninhado até ao absurdo: texto corrido, sem estrutura
        flat = clean_spaces(root.get_text(" "))
        return ([Block(kind="p", text=flat)] if flat else []), []
    return extractor.blocks, extractor.links


class _Extractor:
    """Percorre o DOM e emite blocos de texto; cada elemento de bloco fecha o bloco anterior."""

    def __init__(self, resolve: Callable[[str], tuple[str, str] | None]) -> None:
        self.blocks: list[Block] = []
        self.links: list[Link] = []
        self._resolve = resolve
        self._buffer: list[str] = []
        self._kind = "p"
        self._seen_links: set[tuple[str, int]] = set()
        self._resolved: dict[str, tuple[str, str] | None] = {}
        self._cell = 0  # dentro de uma célula de tabela, listas e parágrafos ficam na mesma linha
        self._deferred: list[Tag] = []

    def defer(self, tag: Tag) -> None:
        self._deferred.append(tag)

    def emit_deferred(self) -> None:
        self.flush()
        pending, self._deferred = self._deferred, []
        for tag in pending:
            self._tag(tag)
        self.flush()

    def flush(self) -> None:
        text = clean_spaces("".join(self._buffer))
        self._buffer = []
        if text:
            self.blocks.append(Block(kind=self._kind, text=text))

    def walk(self, node: Tag | BeautifulSoup) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if type(child) is NavigableString:  # exclui comentários, CDATA, doctype, …
                    self._buffer.append(str(child))
            elif isinstance(child, Tag):
                self._tag(child)

    def _tag(self, tag: Tag) -> None:
        name = tag.name
        if name in _HEADINGS:
            self.flush()
            level = _HEADINGS[name]
            text = clean_spaces(tag.get_text(" "))
            if level > 1 and text:  # o h1 é o título do artigo, que vem à parte
                self.emit_deferred()
                self.blocks.append(Block(kind="h", text=text, level=level))
            return
        if self._cell and (name == "li" or name in _BLOCK_TAGS):
            self._inline_in_cell(tag)
        elif name == "li":
            self.flush()
            previous, self._kind = self._kind, "li"
            self.walk(tag)
            self.flush()
            self._kind = previous
        elif name in _BLOCK_TAGS:
            self.flush()
            self.walk(tag)
            self.flush()
        elif name in ("td", "th"):
            if clean_spaces("".join(self._buffer)):
                self._buffer.append(" | ")
            self._cell += 1
            self.walk(tag)
            self._cell -= 1
        elif name == "br":
            self._buffer.append(" ")
        elif name == "a":
            self._link(tag)
            self.walk(tag)
        else:
            self.walk(tag)

    def _inline_in_cell(self, tag: Tag) -> None:
        """Itens e parágrafos de uma célula juntam-se numa linha: 'Géneros | R&B, soul, blues'."""
        so_far = clean_spaces("".join(self._buffer))
        if so_far and not so_far.endswith("|"):
            self._buffer = [so_far, ", " if tag.name == "li" else " "]
        self.walk(tag)

    def _link(self, tag: Tag) -> None:
        href = tag.get("href")
        if not isinstance(href, str):
            return
        if href not in self._resolved:
            self._resolved[href] = self._resolve(href)
        target = self._resolved[href]
        if target is None:
            return
        key = (target[0], len(self.blocks))
        if key not in self._seen_links:
            self._seen_links.add(key)
            self.links.append(Link(path=target[0], title=target[1], block=len(self.blocks)))
