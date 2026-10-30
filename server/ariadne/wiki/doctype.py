"""Tipo de documento antes de importar para o Wiki.js (docs/WIKI-IMPORT.md, secção 4).

`classify` decide se um item do ZIM é um `article` (vai para o Wiki.js), um `book` (livro ou manual
inteiro: segue para o Calibre/Kavita) ou um `skip` (não é um artigo: perfis, índices, capas,
páginas-concha, simuladores). O motivo fica registado em `reason` para o dashboard e para afinar as
regras. Só regras determinísticas: nenhum modelo, nenhum acesso à rede ou à base de dados.
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

# Menos texto do que isto não é um artigo (concha, página vazia, ficha sem conteúdo).
MIN_TEXT = 300
# A partir daqui um HTML pode ser um livro: com byline de autor ou com capítulos numerados.
BOOK_TEXT = 60_000
# Capítulos/secções numerados distintos que fazem de um HTML longo um livro.
BOOK_CHAPTERS = 9
# Um HTML tão longo que nenhum artigo o é (os maiores artigos de enciclopédia ficam bem abaixo).
HUGE_TEXT = 300_000
# A byline procura-se no início do texto («Por E. Christopher Cone», «By A. Author»).
BYLINE_WINDOW = 3_000
# Páginas curtas feitas só de ligações (índices de cursos, listas de filhos) não são artigos.
LINKS_MAX_TEXT = 3_000
LINKS_RATIO = 0.85

# Uma página de listagem (categoria, arquivo, página N de um blogue): vários resumos de artigos
# com «Read more» ou uma fila de títulos que são ligações para outros artigos.
READ_MORE_MIN = 3  # ligações «Continue reading»/«Read more» a partir das quais é uma listagem
LISTING_RATIO = 0.2  # fração do texto dentro de ligações (um artigo normal fica abaixo de 10%)
LISTING_HEADINGS = 6  # títulos que são ligações
LISTING_PARAGRAPH = 250  # e parágrafos médios mais curtos que isto (resumos)

Kind = Literal["article", "book", "skip"]


@dataclass(frozen=True)
class DocKind:
    kind: Kind
    reason: str


_BOOK_MIMETYPES = {
    "application/pdf": "PDF",
    "application/epub+zip": "EPUB",
    "image/vnd.djvu": "DjVu",
    "application/x-mobipocket-ebook": "MOBI",
}
_TEXT_MIMETYPES = ("text/html", "application/xhtml+xml", "text/plain")
_BOOK_EXTENSIONS = {".pdf": "PDF", ".epub": "EPUB", ".djvu": "DjVu", ".mobi": "MOBI"}
_MEDIA_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".mp3", ".mp4", ".webm", ".ogg", ".ogv", ".oga", ".wav", ".flac", ".m4a", ".mov",
    ".js", ".css", ".woff", ".woff2", ".ttf", ".zip",
)  # fmt: skip

# Espaços de nomes de wiki que não são artigos (`Category:`, `Utilizador:`, `Template:`…), em
# qualquer segmento do caminho (`A/Category:X`). Os projetos e os de documentação técnica
# (`Tag:`, `Key:`, `Handbook:`) não entram: esses são conteúdo.
_SKIP_NAMESPACES = re.compile(
    r"^(?:category|categoria|catégorie|kategorie|categoría|special|especial|spécial|spezial"
    r"|template|predefinição|modèle|vorlage|plantilla|talk|discussão|discussion|diskussion"
    r"|user|utilizador|usuário|utilisateur|benutzer|usuario|user[ _]talk|file|ficheiro|fichier"
    r"|datei|archivo|image|imagem|mediawiki|module|módulo|help|ajuda|aide|hilfe|ayuda"
    r"|wikibooks|wikipedia|wikiversity|wikisource|wiktionary|wikivoyage|wikiquote|project"
    r"|projeto|portal)\s*:",
    re.IGNORECASE,
)
# Os espaços de nomes das digitalizações do wikisource: um fragmento de página de um livro.
_WIKISOURCE_SCANS = re.compile(r"^(?:page|index|página|pagina|livre|seite)\s*:", re.IGNORECASE)
# Paginação e arquivo: `/page/2/`, `page-3.html`, `?page=4`, `/archive/`, `/category/x/`,
# `/author/x/`, `/blog/2021/05/`. `page_123` (libretexts) é uma página de conteúdo, não entra.
_PAGINATION = re.compile(r"(?:^|/)page[/-]\d+(?:\.html?)?/?$|[?&](?:page|paged|pg|offset)=\d+")
_ARCHIVE = re.compile(
    r"(?:^|/)(?:archives?|arquivos?|archiv|archivio)(?:/|$)"
    r"|(?:^|/)(?:categor(?:y|ies|ia|ias|ie)|kategorie|author|authors|autor)/[^/]+(?:/|$)"
    r"|/(?:19|20)\d\d/(?:0?[1-9]|1[0-2])(?:/(?:0?[1-9]|[12]\d|3[01]))?/?$",
    re.IGNORECASE,
)
_READ_MORE = re.compile(
    r"^(?:read more|continue reading|keep reading|read on|read the (?:full )?(?:post|article)"
    r"|leia mais|ler mais|continuar a ler|lire la suite|lire la suite|weiterlesen|leer más"
    r"|seguir leyendo|ver mais|mehr lesen)\b",
    re.IGNORECASE,
)
_USERS = re.compile(r"(?:^|/)users?/(?:\d+(?:/|$)|$)")  # /users/137545/nome (StackExchange)
_TAG_LISTS = re.compile(r"(?:^|/)(?:tags?|questions/tagged|tagged)(?:/|$)")
_INDEXES = re.compile(r"(?:^|/)(?:genindex[^/]*|py-modindex|modindex|search|contents)(?:\.html?)?$")
_DISAMBIG_PATH = re.compile(
    r"\((?:disambiguation|desambiguação|desambiguação|désambiguïsation|begriffsklärung"
    r"|desambiguación|disambigua)\)",
    re.IGNORECASE,
)
_DISAMBIG_HTML = re.compile(r"disambigbox|dmbox-disambig|PageProp/disambiguation", re.IGNORECASE)
_COVER = re.compile(r"(?:^|[_\-./ ])cover(?:\.\d+)?$", re.IGNORECASE)
# Capítulo de livro no wikisource: `…/Chapter_12`, `…/Capítulo_3`, `…/Book_II`.
_WIKISOURCE_CHAPTER = re.compile(
    r"/(?:chapter|chap|capítulo|capitulo|chapitre|kapitel|capitolo|hoofdstuk|book|livro|livre"
    r"|part|parte|partie|teil|canto|act|ato|acte|scene|section|letter)[ _.-]*(?:\d+|[ivxlc]+)"
    r"(?:[ _.-][^/]*)?$",
    re.IGNORECASE,
)

# Fontes que são, por natureza, bibliotecas de livros.
_BOOK_SOURCES = ("gutenberg", "survivorlibrary")
_SIMULATOR_SOURCES = ("phet",)

_REFRESH = re.compile(r"<meta[^>]+http-equiv=[\"']?refresh", re.IGNORECASE)
_BYLINE = re.compile(
    r"\b(?:Por|By|Par|Von|Door|Di|Autor|Author)[:\s]+"
    r"[A-ZÀ-Ý][\w.'’-]*(?:\s+[A-ZÀ-Ý][\w.'’-]*){1,3}"
)
# Capítulos só contam quando são títulos (`<h1>`…`<h6>`): no corpo, «Chapter 20» é uma citação
# (os artigos da IEP citam capítulos de obras clássicas).
_HEADING = re.compile(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_CHAPTER_HEADING = re.compile(
    r"^\s*(?:chapter|capítulo|capitulo|chapitre|kapitel|capitolo|hoofdstuk)\s+"
    r"(\d{1,3}|[IVXLC]{1,6})\b",
    re.IGNORECASE,
)


def classify(*, path: str, mimetype: str, html: str | None, text: str, source_name: str) -> DocKind:
    """O tipo do item `path` da fonte `source_name`. `text` é o texto visível (já sem menus e
    avisos: `ariadne.sources.zim.content_root(html).get_text(" ")`); `html` pode faltar (PDF)."""
    mime = (mimetype or "").split(";")[0].strip().lower()
    decoded = unquote(path).strip("/")
    lowered = decoded.lower()
    source = source_name.lower()

    book = book_by_format(mime, lowered)
    if book is not None:
        return book
    if html is None and mime and not mime.startswith(_TEXT_MIMETYPES):
        return DocKind("skip", f"media or non-text file ({mime})")

    skip = _skip_by_path(decoded, lowered, source, html)
    if skip is not None:
        return skip

    if any(name in source for name in _BOOK_SOURCES):
        if _COVER.search(lowered):
            return DocKind("skip", "book cover")
        if len(text) < MIN_TEXT:
            return DocKind("skip", "book record or cover (no text)")
        return DocKind("book", f"book source ({_matching(source, _BOOK_SOURCES)})")

    if "wikisource" in source and _WIKISOURCE_CHAPTER.search("/" + decoded):
        return DocKind("book", "Wikisource book chapter")

    size = len(text)
    if size < MIN_TEXT:
        if html is not None and _REFRESH.search(html):
            return DocKind("skip", "shell page (meta redirect)")
        return DocKind("skip", f"very little text ({size} characters)")

    if html is not None and size < LINKS_MAX_TEXT and _mostly_links(html, size):
        return DocKind("skip", "list of links (index with no content of its own)")
    if html is not None and (listing := _listing(html, size)) is not None:
        return DocKind("skip", listing)

    if size >= BOOK_TEXT:
        head = text[:BYLINE_WINDOW]
        byline = _BYLINE.search(head)
        if byline is not None:
            return DocKind("book", f"long text ({size // 1000}k characters) with a byline")
        if html is not None and _chapter_headings(html) >= BOOK_CHAPTERS:
            return DocKind("book", f"long text ({size // 1000}k characters) with chapters")
        if size >= HUGE_TEXT:
            return DocKind("book", f"very long text ({size // 1000}k characters)")
    return DocKind("article", "article")


def _matching(source: str, names: tuple[str, ...]) -> str:
    return next(name for name in names if name in source)


def book_by_format(mime: str, lowered_path: str) -> DocKind | None:
    """`book` se o item é um PDF, EPUB, DjVu ou MOBI (pelo tipo MIME ou pela extensão)."""
    mime = (mime or "").split(";")[0].strip().lower()
    lowered_path = lowered_path.lower()
    if mime in _BOOK_MIMETYPES:
        return DocKind("book", f"{_BOOK_MIMETYPES[mime]} file")
    for extension, label in _BOOK_EXTENSIONS.items():
        if lowered_path.endswith(extension):
            return DocKind("book", f"{label} file")
    return None


def _skip_by_path(decoded: str, lowered: str, source: str, html: str | None) -> DocKind | None:
    if lowered.endswith(_MEDIA_EXTENSIONS):
        return DocKind("skip", "media or code file")
    if any(name in source for name in _SIMULATOR_SOURCES):
        return DocKind("skip", "simulator (application, not text)")
    if _USERS.search(lowered):
        return DocKind("skip", "user profile")
    if _TAG_LISTS.search(lowered):
        return DocKind("skip", "tag list")
    if _PAGINATION.search(lowered):
        return DocKind("skip", "pagination page (listing)")
    if _ARCHIVE.search(lowered):
        return DocKind("skip", "archive or category (listing)")
    if _INDEXES.search(lowered):
        return DocKind("skip", "generated index (genindex and the like)")
    if _DISAMBIG_PATH.search(lowered):
        return DocKind("skip", "disambiguation")
    scans = "wikisource" in source
    for segment in decoded.split("/"):
        if _SKIP_NAMESPACES.match(segment):
            return DocKind("skip", f"namespace that is not an article ({segment.split(':')[0]}:)")
        if scans and _WIKISOURCE_SCANS.match(segment):
            return DocKind("skip", "Wikisource scan page")
    if html is not None and _DISAMBIG_HTML.search(html[:200_000]):
        return DocKind("skip", "disambiguation")
    return None


def _chapter_headings(html: str) -> int:
    """Quantos capítulos distintos («Capítulo 3», «Chapter IV») aparecem como título."""
    found = set()
    for match in _HEADING.finditer(html):
        chapter = _CHAPTER_HEADING.match(_TAG.sub("", match.group(1)))
        if chapter is not None:
            found.add(chapter.group(1).upper())
    return len(found)


def _mostly_links(html: str, size: int) -> bool:
    """Quase todo o texto visível está dentro de ligações. Só se chama em páginas curtas."""
    from ariadne.sources.zim import content_root  # tarde: o libzim só carrega quando é preciso

    root = content_root(html)
    link_chars = sum(len(" ".join(a.get_text(" ").split())) for a in root.find_all("a"))
    return link_chars / max(size, 1) >= LINKS_RATIO


def _listing(html: str, size: int) -> str | None:
    """O motivo de a página ser uma listagem de artigos (categoria, arquivo, página N de um blogue),
    ou `None`: três ou mais «Read more» e muito texto dentro de ligações, ou uma fila de títulos que
    são ligações com resumos curtos. Um artigo normal tem poucas ligações e parágrafos compridos."""
    from ariadne.sources.zim import content_root  # tarde: o libzim só carrega quando é preciso

    root = content_root(html)
    anchors = root.find_all("a")
    link_chars = sum(len(" ".join(a.get_text(" ").split())) for a in anchors)
    ratio = link_chars / max(size, 1)
    if ratio < LISTING_RATIO:
        return None
    read_more = sum(1 for a in anchors if _READ_MORE.match(" ".join(a.get_text(" ").split())))
    if read_more >= READ_MORE_MIN:
        return (
            f"listing of summaries ({read_more} 'read more' links, "
            f"{ratio:.0%} of the text inside links)"
        )
    linked = [h for h in root.find_all(["h1", "h2", "h3", "h4"]) if h.find("a") is not None]
    paragraphs = [len(p.get_text()) for p in root.find_all("p")]
    average = sum(paragraphs) / len(paragraphs) if paragraphs else 0
    if len(linked) >= LISTING_HEADINGS and average < LISTING_PARAGRAPH:
        return f"listing of articles ({len(linked)} linked headings, {ratio:.0%} inside links)"
    return None
