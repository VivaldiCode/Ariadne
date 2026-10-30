"""Extração de livros por capítulos: EPUB (zip + bs4) e PDF (pypdf), sem dependências nativas.

Cada livro dá uma lista de capítulos com HTML já saneado (a mesma lista de permissões das lições,
`courses.render.sanitize_html`) e uma nota a dizer **como** se dividiu (índice do EPUB, marcadores
do PDF, o padrão «Capítulo N» ou janelas de páginas). A extração é determinística: o mesmo ficheiro
dá sempre o mesmo texto e as mesmas posições, que é o que torna estável a citação
`<livro>/<capítulo>#<posição>`; se o algoritmo mudar, sobe `EXTRACTOR_VERSION` e o `books sync`
volta a extrair.

**Sem DRM** (STRUCTURE.md, secções 1 e 7): `check_drm` deteta EPUB com Adobe ADEPT, FairPlay ou
outras cifras no conteúdo, PDF cifrado e MOBI/AZW cifrado, e a entrada recusa-os com uma mensagem
clara. Nada tenta contornar nada.
"""

import codecs
import dataclasses
import hashlib
import html as htmllib
import inspect
import json
import logging
import math
import posixpath
import re
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup
from bs4.element import Tag

from ariadne.courses.render import sanitize_html
from ariadne.wiki.convert import hidden_texts

EXTRACTOR_VERSION = 2

logging.getLogger("pypdf").setLevel(logging.ERROR)  # avisos de fontes e objetos: ruído

# Limites contra ficheiros hostis (zip bombs, XML gigante): o que passa deles recusa-se.
# O limite por ficheiro do EPUB mediu-se com o bs4: 8 MB de HTML levam ~10 s a ler e sanear.
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 10_000  # ficheiros num EPUB
MAX_MIMETYPE_BYTES = 64
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
# A extração corre num subprocesso com estes limites (memória virtual e tempo de CPU/relógio).
EXTRACT_MEMORY_MB = 2048
EXTRACT_TIMEOUT = 300
MAX_RATIO = 400
MAX_PDF_PAGES = 20_000
MAX_CHAPTERS = 5_000
MIN_FRONT_MATTER_CHARS = 400  # o que vem antes do 1.º capítulo só conta se tem algum texto
MEDIAN_CHAPTER_RAW = 120_000  # tamanho de HTML a partir do qual um capítulo é «grande demais»
PDF_WINDOW = 10  # páginas por capítulo quando o PDF não traz estrutura nenhuma
SPLIT_ABOVE = 60_000  # um capítulo de EPUB com mais texto que isto parte-se (índice pobre)
SPLIT_TARGET = 30_000  # tamanho aproximado de cada parte
SPLIT_MIN = 5_000  # uma parte só começa num título se a anterior já tem este texto

# Cifras de **fontes** (ofuscação), que o EPUB permite: não são DRM.
FONT_OBFUSCATION = frozenset(
    {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
)
_CHAPTER_HEADING = re.compile(
    r"^(chapter|cap[ií]tulo|chapitre|kapitel|part|parte)\s+([0-9]{1,3}|[ivxlc]{1,8})\b",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^a-z0-9]+")
_BOILERPLATE_TITLE = re.compile(
    r"project gutenberg.*licen[sc]e|licen[sc]e.*project gutenberg", re.I
)
# Caracteres de controlo (menos \t, \n, \r): não ficam no texto. A pesquisa usa \x01 e \x02 para
# marcar o excerto, e o texto original nunca os traz.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


class BookError(Exception):
    """O ficheiro não se pode ler como livro (estragado, formato desconhecido, perigoso)."""


class DrmError(BookError):
    """O livro tem DRM. O Ariadne não aceita conteúdo cuja cópia exija contornar proteções."""


# --- estruturas ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class BookMeta:
    title: str = ""
    authors: tuple[str, ...] = ()
    language: str | None = None
    publisher: str | None = None
    published: str | None = None
    description: str = ""
    identifiers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedChapter:
    path: str  # o caminho do documento dentro da fonte: «003-o-escandalo»
    title: str
    html: str  # saneado (lista de permissões), sem o título
    href: str | None = None  # EPUB: o ficheiro onde o capítulo começa
    start_page: int | None = None  # PDF: a contar de 1
    end_page: int | None = None
    hidden: tuple[str, ...] = ()  # texto escondido por CSS que se tirou (sinal para a curadoria)


@dataclass(frozen=True)
class ExtractedBook:
    format: str  # 'epub' | 'pdf'
    meta: BookMeta
    chapters: tuple[ExtractedChapter, ...]
    note: str  # como se dividiu em capítulos
    pages: int | None = None


# --- deteção do formato e do DRM -----------------------------------------------------------------


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BookError(f"Corrupt zip file: {exc}") from exc
    if len(archive.infolist()) > MAX_ENTRIES:
        archive.close()
        raise BookError(f"EPUB has too many files (maximum {MAX_ENTRIES}).")
    return archive


def sniff_format(path: Path) -> str:
    """`epub`, `pdf` ou `mobi`, pelo conteúdo e não pela extensão."""
    with path.open("rb") as handle:
        head = handle.read(80)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        with _open_zip(path) as archive:
            mimetype = b""
            try:
                info = archive.getinfo("mimetype")
            except KeyError:
                info = None
            if info is not None and info.file_size <= MAX_MIMETYPE_BYTES:  # nunca se lê um gigante
                mimetype = archive.read(info)
            try:
                archive.getinfo("META-INF/container.xml")
                has_container = True
            except KeyError:
                has_container = False
        if mimetype.strip() == b"application/epub+zip" or has_container:
            return "epub"
        raise BookError(
            "It is a zip but not an EPUB (the application/epub+zip mimetype is missing)."
        )
    if head[60:68] in (b"BOOKMOBI", b"TEXtREAd"):
        return "mobi"
    raise BookError("Unsupported format: only EPUB, PDF and MOBI (converted to EPUB).")


def check_drm(path: Path, fmt: str | None = None) -> None:
    """Levanta `DrmError` se o ficheiro está cifrado ou protegido por DRM. Corre **antes** de o
    livro tocar no Calibre ou no índice."""
    fmt = fmt or sniff_format(path)
    if fmt == "pdf":
        _check_pdf_drm(path)
    elif fmt == "epub":
        _check_epub_drm(path)
    elif fmt == "mobi":
        _check_mobi_drm(path)


def _check_pdf_drm(path: Path) -> None:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(str(path))
        encrypted = reader.is_encrypted
    except (PyPdfError, OSError, ValueError) as exc:
        raise BookError(f"Unreadable PDF: {exc}") from exc
    if encrypted:
        raise DrmError(
            "Encrypted PDF (it has a password or copy restrictions). Ariadne does not accept "
            "protected content: use an unprotected copy that you have the right to have."
        )


def _check_epub_drm(path: Path) -> None:
    try:
        with _open_zip(path) as archive:
            names = set(archive.namelist())
            if "META-INF/rights.xml" in names:
                raise DrmError(
                    "EPUB with DRM (Adobe ADEPT: META-INF/rights.xml). Ariadne does not accept "
                    "protected books and does not circumvent technical protections."
                )
            if "META-INF/sinf.xml" in names or any(n.startswith("META-INF/sinf") for n in names):
                raise DrmError(
                    "EPUB with DRM (Apple FairPlay). Ariadne does not accept books with DRM."
                )
            if "META-INF/encryption.xml" in names:
                root = _xml(_read_member(archive, "META-INF/encryption.xml"))
                for method in root.iterfind(".//{*}EncryptionMethod"):
                    algorithm = method.get("Algorithm", "")
                    if algorithm and algorithm not in FONT_OBFUSCATION:
                        raise DrmError(
                            "Encrypted EPUB (the content is protected: "
                            f"{algorithm.rsplit('#', 1)[-1] or algorithm}). "
                            "Ariadne does not accept books with DRM."
                        )
    except zipfile.BadZipFile as exc:
        raise BookError(f"Corrupt EPUB: {exc}") from exc


def _check_mobi_drm(path: Path) -> None:
    """Palm DB: o tipo de cifra está nos bytes 12-13 do registo 0 (0 = nenhuma). Os ficheiros
    Kindle (AZW/KFX) com DRM têm-no diferente de zero."""
    with path.open("rb") as handle:
        header = handle.read(78 + 8)
        if len(header) < 86:
            raise BookError("Corrupt MOBI: header too short.")
        offset = int.from_bytes(header[78:82], "big")
        handle.seek(offset)
        record = handle.read(16)
    if len(record) < 14:
        raise BookError("Corrupt MOBI: record 0 too short.")
    encryption = int.from_bytes(record[12:14], "big")
    if encryption != 0:
        raise DrmError(
            "MOBI/Kindle with DRM (Mobipocket/Amazon encryption). Ariadne does not accept "
            "protected books and does not circumvent technical protections."
        )


# --- utilitários ---------------------------------------------------------------------------------


def slugify(text: str, limit: int = 48) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return _NON_WORD.sub("-", plain.lower()).strip("-")[:limit].strip("-")


def chapter_path(position: int, title: str) -> str:
    slug = slugify(title)
    return f"{position:03d}-{slug}" if slug else f"{position:03d}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise BookError(f"EPUB without {name}.") from exc
    if info.file_size > MAX_MEMBER_BYTES:
        raise BookError(f"EPUB with a huge file ({name}: {info.file_size} bytes).")
    if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
        raise BookError(f"Suspicious EPUB: {name} decompresses {MAX_RATIO}× or more.")
    with archive.open(info) as handle:
        data = handle.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise BookError(f"EPUB with a huge file ({name}).")
    return data


_ENTITY = re.compile(r"<!ENTITY", re.IGNORECASE)
_XML_DECLARED = re.compile(rb"""^\s*<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)["']""")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9._-]+)""", re.IGNORECASE)


def _decode(data: bytes) -> str:
    """O texto de um ficheiro do EPUB: respeita a BOM, depois a declaração XML e o charset do
    `<meta>`; UTF-16 só com BOM (declarado sem BOM, um ficheiro UTF-8 sairia em lixo). Sem nada
    disso, UTF-8 e, se não for, cp1252."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace")
    head = data[:1024]
    declared = _XML_DECLARED.match(head) or _META_CHARSET.search(head)
    if declared is not None:
        name = declared.group(1).decode("ascii", "ignore")
        try:
            codec = codecs.lookup(name).name
        except LookupError:
            codec = ""
        if codec and not codec.startswith(("utf-16", "utf-32")):
            return data.decode(codec, "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def _xml(data: bytes) -> ET.Element:
    """XML pequeno e sem entidades: o texto descodifica-se **antes** de testar (um UTF-16 com
    `<!ENTITY` não passava num teste de bytes)."""
    text = _decode(data)
    if _ENTITY.search(text):
        raise BookError("XML with declared entities: refused (expansion attack).")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise BookError(f"Corrupt XML: {exc}") from exc


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return _SPACES.sub(" ", "".join(element.itertext())).strip()


def _attr(element: ET.Element, name: str) -> str:
    """Um atributo, com ou sem espaço de nomes (`opf:role` aparece como `{ns}role`)."""
    for key, value in element.attrib.items():
        if key == name or key.endswith("}" + name):
            return value
    return ""


def _plain(html: str) -> str:
    return _SPACES.sub(" ", BeautifulSoup(html, "html.parser").get_text(" ")).strip()


# --- EPUB ----------------------------------------------------------------------------------------


@dataclass
class _TocEntry:
    title: str
    path: str  # ficheiro no zip
    fragment: str
    depth: int


def extract_epub(path: Path) -> ExtractedBook:
    check_drm(path, "epub")
    try:
        with _open_zip(path) as archive:
            return _extract_epub(archive)
    except zipfile.BadZipFile as exc:
        raise BookError(f"Corrupt EPUB: {exc}") from exc


def _extract_epub(archive: zipfile.ZipFile) -> ExtractedBook:
    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_TOTAL_BYTES:
        raise BookError(f"EPUB too large once decompressed ({total} bytes).")
    container = _xml(_read_member(archive, "META-INF/container.xml"))
    rootfile = container.find(".//{*}rootfile")
    opf_path = rootfile.get("full-path", "") if rootfile is not None else ""
    if not opf_path:
        raise BookError("EPUB without a package file (container.xml has no rootfile).")
    opf = _xml(_read_member(archive, opf_path))
    base = posixpath.dirname(opf_path)

    def resolve(href: str) -> str | None:
        target = posixpath.normpath(posixpath.join(base, unquote(href.split("#", 1)[0])))
        return None if target.startswith("..") or target.startswith("/") else target

    manifest: dict[str, dict[str, str]] = {}
    for item in opf.iterfind(".//{*}manifest/{*}item"):
        item_id = item.get("id")
        href = item.get("href")
        if item_id and href and (target := resolve(href)):
            manifest[item_id] = {
                "path": target,
                "type": item.get("media-type", ""),
                "properties": item.get("properties", ""),
            }
    spine_ids = [r.get("idref", "") for r in opf.iterfind(".//{*}spine/{*}itemref")]
    names = set(archive.namelist())  # uma vez: não dentro do ciclo da spine
    spine: list[str] = []
    for ref in spine_ids:
        entry = manifest.get(ref)
        if (
            entry
            and entry["type"] in ("application/xhtml+xml", "text/html")
            and (entry["path"] in names)
        ):
            spine.append(entry["path"])
    if not spine:
        raise BookError("EPUB without readable content (the spine is empty).")

    meta = _epub_meta(opf)
    raw: dict[str, str] = {
        name: _decode(_read_member(archive, name)) for name in dict.fromkeys(spine)
    }
    toc = _epub_toc(archive, opf, manifest, names)
    chapters, note = _epub_chapters(spine, raw, toc, meta.title)
    chapters, note = _refine_chapters(chapters, note)
    if len(chapters) > MAX_CHAPTERS:
        raise BookError(f"EPUB with {len(chapters)} chapters: too many.")
    if not chapters:
        raise BookError("EPUB without text: no chapter has content.")
    return ExtractedBook("epub", meta, tuple(chapters), note)


def _epub_meta(opf: ET.Element) -> BookMeta:
    metadata = opf.find(".//{*}metadata")
    if metadata is None:
        return BookMeta()

    def first(tag: str) -> str:
        return _text(metadata.find(f"{{*}}{tag}"))

    authors = tuple(
        name
        for creator in metadata.iterfind("{*}creator")
        if (name := _text(creator)) and (_attr(creator, "role") or "aut") == "aut"
    )
    identifiers: dict[str, str] = {}
    for ident in metadata.iterfind("{*}identifier"):
        value = _text(ident)
        scheme = _attr(ident, "scheme").lower()
        lowered = value.lower()
        if lowered.startswith("urn:isbn:") or scheme == "isbn":
            identifiers["isbn"] = value.split(":")[-1]
        elif lowered.startswith("urn:uuid:") or scheme == "uuid":
            identifiers["uuid"] = value.split(":")[-1]
        elif "gutenberg.org" in lowered:
            identifiers["gutenberg"] = value.rstrip("/").split("/")[-1]
        elif value and "id" not in identifiers:
            identifiers["id"] = value
    description = BeautifulSoup(first("description"), "html.parser").get_text(" ")
    return BookMeta(
        title=first("title"),
        authors=authors,
        language=(first("language").split("-")[0].lower() or None),
        publisher=first("publisher") or None,
        published=first("date") or None,
        description=_SPACES.sub(" ", description).strip(),
        identifiers=identifiers,
    )


@dataclass(frozen=True)
class EpubCredits:
    """O que o pacote OPF diz de quem fez o livro, sem extrair nada (`read_epub_credits`)."""

    title: str
    authors: tuple[str, ...]
    translators: tuple[str, ...]
    language: str | None
    identifiers: dict[str, str]


def read_epub_credits(path: Path) -> EpubCredits:
    """Só o OPF: título, autores, **tradutores**, língua e identificadores. Serve para decidir a
    obra (`books/works.py`) antes de o livro ir para o Calibre. O tradutor é um `dc:contributor`
    com `opf:role="trl"` (EPUB 2) ou um criador/contribuidor cujo `id` uma `<meta property="role"
    refines="#id">trl</meta>` aponta (EPUB 3). Lê um só ficheiro pequeno, com os mesmos limites e
    a mesma recusa de entidades XML que a extração."""
    try:
        with _open_zip(path) as archive:
            container = _xml(_read_member(archive, "META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            opf_path = rootfile.get("full-path", "") if rootfile is not None else ""
            if not opf_path:
                raise BookError("EPUB without a package file (container.xml has no rootfile).")
            opf = _xml(_read_member(archive, opf_path))
    except zipfile.BadZipFile as exc:
        raise BookError(f"Corrupt EPUB: {exc}") from exc
    meta = _epub_meta(opf)
    metadata = opf.find(".//{*}metadata")
    translators: list[str] = []
    if metadata is not None:
        refined = {
            _attr(node, "refines").lstrip("#")
            for node in metadata.iterfind("{*}meta")
            if _attr(node, "property") == "role" and _text(node).lower() == "trl"
        }
        for node in [*metadata.iterfind("{*}contributor"), *metadata.iterfind("{*}creator")]:
            name = _text(node)
            if name and (_attr(node, "role").lower() == "trl" or node.get("id") in refined):
                translators.append(name)
    authors = tuple(a for a in meta.authors if a not in translators)
    identifiers = dict(meta.identifiers)
    if metadata is not None:  # `wikidata:Q123`, `https://www.wikidata.org/entity/Q123`
        for node in metadata.iterfind("{*}identifier"):
            value = _text(node)
            found = re.search(r"\bQ[1-9]\d*\b", value)
            if found and "wikidata" in value.casefold():
                identifiers["wikidata"] = found.group(0)
    return EpubCredits(
        meta.title, authors, tuple(dict.fromkeys(translators)), meta.language, identifiers
    )


def _epub_toc(
    archive: zipfile.ZipFile,
    opf: ET.Element,
    manifest: dict[str, dict[str, str]],
    names: set[str],
) -> list[_TocEntry]:
    nav = next(
        (m for m in manifest.values() if "nav" in m["properties"].split() and m["path"] in names),
        None,
    )
    if nav is not None:
        try:
            entries = _toc_from_nav(_decode(_read_member(archive, nav["path"])), nav["path"])
            if entries:
                return entries
        except BookError:
            pass
    spine = opf.find(".//{*}spine")
    ncx_id = spine.get("toc") if spine is not None else None
    ncx = manifest.get(ncx_id or "") or next(
        (m for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), None
    )
    if ncx is not None and ncx["path"] in names:
        try:
            return _toc_from_ncx(_read_member(archive, ncx["path"]), ncx["path"])
        except BookError:
            return []
    return []


def _resolve_relative(owner: str, href: str) -> tuple[str, str] | None:
    path, _, fragment = href.partition("#")
    target = (
        posixpath.normpath(posixpath.join(posixpath.dirname(owner), unquote(path or ".")))
        if (path)
        else owner
    )
    if target.startswith("..") or target.startswith("/"):
        return None
    return target, unquote(fragment)


def _toc_from_nav(html: str, owner: str) -> list[_TocEntry]:
    soup = BeautifulSoup(html, "html.parser")
    nav = next(
        (
            n
            for n in soup.find_all("nav")
            if "toc" in str(n.get("epub:type", "") or n.get("type", "")).split()
        ),
        soup.find("nav"),
    )
    if not isinstance(nav, Tag):
        return []
    entries: list[_TocEntry] = []

    def walk(ol: Tag, depth: int) -> None:
        for li in ol.find_all("li", recursive=False):
            link = li.find(["a", "span"], recursive=False)
            if isinstance(link, Tag):
                title = _SPACES.sub(" ", link.get_text(" ")).strip()
                href = link.get("href") if link.name == "a" else None
                target = _resolve_relative(owner, str(href)) if href else None
                if title and target:
                    entries.append(_TocEntry(title, target[0], target[1], depth))
            child = li.find(["ol", "ul"], recursive=False)
            if isinstance(child, Tag):
                walk(child, depth + 1)

    root = nav.find(["ol", "ul"])
    if isinstance(root, Tag):
        walk(root, 1)
    return entries


def _toc_from_ncx(data: bytes, owner: str) -> list[_TocEntry]:
    root = _xml(data)
    entries: list[_TocEntry] = []

    def walk(parent: ET.Element, depth: int) -> None:
        for point in parent.findall("{*}navPoint"):
            title = _text(point.find("{*}navLabel"))
            content = point.find("{*}content")
            src = content.get("src", "") if content is not None else ""
            target = _resolve_relative(owner, src) if src else None
            if title and target:
                entries.append(_TocEntry(title, target[0], target[1], depth))
            walk(point, depth + 1)

    nav_map = root.find("{*}navMap")
    if nav_map is not None:
        walk(nav_map, 1)
    return entries


def _fragment_offset(html: str, fragment: str) -> int:
    """Onde começa, no HTML bruto, o elemento com este `id` (ou `<a name>`): recua até ao `<` da
    etiqueta. 0 se não se encontra (o capítulo começa no início do ficheiro)."""
    if not fragment:
        return 0
    quoted = re.escape(fragment)
    match = re.search(rf"""\b(?:id|name)\s*=\s*["']{quoted}["']""", html)
    if match is None:
        return 0
    start = html.rfind("<", 0, match.start())
    return max(start, 0)


@dataclass
class _Boundary:
    title: str
    file_index: int
    offset: int
    href: str


def _epub_chapters(
    spine: list[str], raw: dict[str, str], toc: list[_TocEntry], book_title: str
) -> tuple[list[ExtractedChapter], str]:
    index_of = {name: i for i, name in enumerate(dict.fromkeys(spine))}
    files = list(index_of)

    def boundaries_for(max_depth: int) -> list[_Boundary]:
        found: list[_Boundary] = []
        for entry in toc:
            if entry.depth > max_depth or entry.path not in index_of:
                continue
            boundary = _Boundary(
                entry.title,
                index_of[entry.path],
                _fragment_offset(raw[entry.path], entry.fragment),
                entry.path,
            )
            if found and (boundary.file_index, boundary.offset) <= (
                found[-1].file_index,
                found[-1].offset,
            ):
                continue  # vários títulos para o mesmo sítio, ou um índice fora de ordem
            found.append(boundary)
        return found

    chosen: list[_Boundary] = []
    depth_used = 0
    max_depth = max((entry.depth for entry in toc), default=0)
    for depth in range(1, min(max_depth, 3) + 1):
        candidate = boundaries_for(depth)
        depth_used = depth
        chosen = candidate
        if len(candidate) >= 2 and _median_raw(candidate, files, raw) <= MEDIAN_CHAPTER_RAW:
            break
    if len(chosen) >= 2:
        chapters = _chapters_from_boundaries(chosen, files, raw)
        note = f"EPUB table of contents (level {depth_used}, {len(chapters)} chapters)"
        return chapters, note
    # Sem índice que sirva: um capítulo por ficheiro da spine com texto.
    chapters = _chapters_from_spine(files, raw, book_title)
    return chapters, f"EPUB spine files ({len(chapters)} chapters; no useful table of contents)"


_XML_PROLOG = re.compile(r"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>", re.IGNORECASE)
_WRAPPER_TAGS = re.compile(r"</?(?:html|body)\b[^>]*>", re.IGNORECASE)


def _unwrapped(html: str) -> str:
    """O pedaço de um ficheiro sem o prólogo XML, o `<html>` e o `<body>`: vários ficheiros
    juntos num só capítulo não podem ficar como vários documentos (o analisador só veria o 1.º
    `<body>`)."""
    return _WRAPPER_TAGS.sub("", _XML_PROLOG.sub("", html))


def _slice(boundaries: list[_Boundary], i: int, files: list[str], raw: dict[str, str]) -> str:
    start = boundaries[i]
    end = boundaries[i + 1] if i + 1 < len(boundaries) else None
    parts: list[str] = []
    last_file = end.file_index if end is not None else len(files) - 1
    for file_index in range(start.file_index, last_file + 1):
        html = raw[files[file_index]]
        lo = start.offset if file_index == start.file_index else 0
        hi = end.offset if end is not None and file_index == end.file_index else len(html)
        parts.append(_unwrapped(html[lo:hi]))
    return "\n".join(parts)


def _median_raw(boundaries: list[_Boundary], files: list[str], raw: dict[str, str]) -> int:
    sizes = sorted(len(_slice(boundaries, i, files, raw)) for i in range(len(boundaries)))
    return sizes[len(sizes) // 2] if sizes else 0


def _chapters_from_boundaries(
    boundaries: list[_Boundary], files: list[str], raw: dict[str, str]
) -> list[ExtractedChapter]:
    # O que vem antes do 1.º capítulo (capa, folha de rosto, prefácio) é matéria à parte.
    first = boundaries[0]
    front = "\n".join(
        _unwrapped(piece)
        for piece in [
            *(raw[files[i]] for i in range(first.file_index)),
            raw[files[first.file_index]][: first.offset],
        ]
    )
    pending: list[tuple[str, str, str]] = []  # (título, html, href)
    if len(_plain(front)) >= MIN_FRONT_MATTER_CHARS:
        pending.append(("Início", front, files[0]))
    for i, boundary in enumerate(boundaries):
        pending.append((boundary.title, _slice(boundaries, i, files, raw), boundary.href))
    chapters: list[ExtractedChapter] = []
    carry = ""
    for title, html, href in pending:
        combined = f"{carry}\n{html}" if carry else html
        body, hidden = _clean_chapter_html(combined, title)
        if len(_plain(body)) < 80 and (title, html, href) != pending[-1]:
            carry = combined  # um título de parte ou uma folha quase vazia: vai para o seguinte
            continue
        carry = ""
        if not _plain(body):
            continue
        chapters.append(
            ExtractedChapter(
                path=chapter_path(len(chapters) + 1, title),
                title=title,
                html=body,
                href=href,
                hidden=hidden,
            )
        )
    return chapters


def _refine_chapters(
    chapters: list[ExtractedChapter], note: str
) -> tuple[list[ExtractedChapter], str]:
    """O que o índice do EPUB deixa por fazer: tira a licença do Project Gutenberg (um «capítulo»
    de rodapé em cada livro, que só polui a pesquisa) e parte os capítulos enormes (um índice com
    duas entradas, autor e licença, deixava o livro todo num só capítulo)."""
    kept = [c for c in chapters if not _BOILERPLATE_TITLE.search(c.title)] or chapters
    dropped = len(chapters) - len(kept)
    refined: list[ExtractedChapter] = []
    split = 0
    for chapter in kept:
        parts = _split_chapter(chapter) if len(_plain(chapter.html)) > SPLIT_ABOVE else [chapter]
        split += len(parts) - 1
        refined.extend(parts)
    if not dropped and not split:
        return chapters, note
    numbered = [
        replace(c, path=chapter_path(number, c.title)) for number, c in enumerate(refined, start=1)
    ]
    extra = ", ".join(
        part
        for part in (
            f"{dropped} license/footer block(s) removed" if dropped else "",
            f"{split} part(s) from large chapters" if split else "",
        )
        if part
    )
    return numbered, f"{note}; {extra}"


def _split_chapter(chapter: ExtractedChapter) -> list[ExtractedChapter]:
    """Parte um capítulo grande: num título interno (h1 a h4) quando a parte já vai grande, ou ao
    chegar a `SPLIT_TARGET` de texto, sempre entre blocos. As partes sem título interno chamam-se
    «<título> (parte N)». O texto é o mesmo, só mudam as fronteiras."""
    soup = BeautifulSoup(chapter.html, "html.parser")
    blocks: list[Any] = list(soup.children)
    while len(blocks) == 1 and isinstance(blocks[0], Tag) and blocks[0].name in ("div", "section"):
        blocks = list(blocks[0].children)  # um invólucro único: parte-se o que está dentro
    pieces: list[tuple[str | None, list[str]]] = [(None, [])]
    size = 0
    for block in blocks:
        text = block.get_text(" ") if isinstance(block, Tag) else str(block)
        heading = isinstance(block, Tag) and block.name in ("h1", "h2", "h3", "h4")
        new_part = size >= SPLIT_TARGET or (heading and size >= SPLIT_MIN)
        if new_part and pieces[-1][1]:
            pieces.append((None, []))
            size = 0
        if heading and not pieces[-1][1] and pieces[-1][0] is None:
            heading_text = _SPACES.sub(" ", text).strip()[:200]
            pieces[-1] = (heading_text or None, [])
            size += len(text)
            continue  # o título passa a ser o da parte (o leitor acrescenta-o)
        # Um texto solto sai com o escape de volta (str() daria um «<script>» a sério); o 2.º título
        # seguido (livro > capítulo) fica no texto da parte em vez de apagar o 1.º.
        pieces[-1][1].append(str(block) if isinstance(block, Tag) else block.output_ready())
        size += len(_SPACES.sub(" ", text))
    kept: list[tuple[str | None, list[str]]] = []
    for title, html in pieces:
        if _plain("".join(html)):
            kept.append((title, html))
        elif title and kept:  # um título sem texto à frente (o último): nada se perde
            kept[-1][1].append(f"<p>{htmllib.escape(title, quote=False)}</p>")
    pieces = kept
    if len(pieces) < 2:
        return [chapter]
    result: list[ExtractedChapter] = []
    for number, (title, html) in enumerate(pieces, start=1):
        fallback = chapter.title if number == 1 else f"{chapter.title} (parte {number})"
        result.append(
            replace(
                chapter,
                title=title or fallback,
                html="".join(html).strip(),
                hidden=chapter.hidden if number == 1 else (),
            )
        )
    return result


def _chapters_from_spine(
    files: list[str], raw: dict[str, str], book_title: str
) -> list[ExtractedChapter]:
    chapters: list[ExtractedChapter] = []
    for name in files:
        soup = BeautifulSoup(raw[name], "html.parser")
        heading = soup.find(["h1", "h2", "h3"])
        title = (
            _SPACES.sub(" ", heading.get_text(" ")).strip()
            if isinstance(heading, Tag)
            else (soup.title.get_text(" ").strip() if soup.title else "")
        ) or f"Secção {len(chapters) + 1}"
        body, hidden = _clean_chapter_html(raw[name], title)
        if len(_plain(body)) < 200:
            continue
        chapters.append(
            ExtractedChapter(
                path=chapter_path(len(chapters) + 1, title),
                title=title,
                html=body,
                href=name,
                hidden=hidden,
            )
        )
    return chapters


def _clean_chapter_html(html: str, title: str) -> tuple[str, tuple[str, ...]]:
    """HTML do capítulo pela lista de permissões, sem o `<head>`/`<title>` e sem o primeiro título
    se repete o do capítulo (o leitor acrescenta o seu). O texto escondido por CSS (cor igual ao
    fundo, tamanho zero, fora do ecrã) não entra no capítulo: sai como segundo valor, para a
    curadoria o assinalar (`hidden_text`) sem chegar ao leitor nem ao índice."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["head", "title", "style", "script", "nav"]):
        tag.decompose()
    hidden = tuple(hidden_texts(soup))
    body = soup.body if soup.body is not None else soup
    first = body.find(["h1", "h2", "h3"])
    if isinstance(first, Tag) and _same_title(first.get_text(" "), title):
        first.decompose()
    try:
        clean = sanitize_html("".join(str(child) for child in body.children))
    except RecursionError:  # HTML aninhado até ao absurdo: fica o texto corrido
        flat = _SPACES.sub(" ", body.get_text(" ")).strip()
        clean = f"<p>{htmllib.escape(flat, quote=False)}</p>" if flat else ""
    return _CONTROL.sub("", clean).strip(), hidden


def _same_title(a: str, b: str) -> bool:
    def norm(text: str) -> str:
        return _NON_WORD.sub("", unicodedata.normalize("NFKD", text).lower())

    return bool(norm(a)) and norm(a) == norm(b)


# --- PDF -----------------------------------------------------------------------------------------


def extract_pdf(path: Path) -> ExtractedBook:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    check_drm(path, "pdf")
    try:
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
    except (PyPdfError, OSError, ValueError) as exc:
        raise BookError(f"Unreadable PDF: {exc}") from exc
    if page_count == 0:
        raise BookError("PDF without pages.")
    if page_count > MAX_PDF_PAGES:
        raise BookError(f"PDF with {page_count} pages: too many.")
    pages: list[str] = []
    failed = 0
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # um objeto estragado numa página não perde o livro
            pages.append("")
            failed += 1
    if not any(p.strip() for p in pages):
        raise BookError("PDF without extractable text (probably scanned, images only): needs OCR.")
    meta = _pdf_meta(reader)
    ranges, how = _pdf_ranges(reader, pages)
    chapters: list[ExtractedChapter] = []
    for title, start, end in ranges:
        paragraphs = _pdf_paragraphs(pages[start - 1 : end])
        if not paragraphs:
            continue
        body = "".join(f"<p>{htmllib.escape(p, quote=False)}</p>" for p in paragraphs)
        chapters.append(
            ExtractedChapter(
                path=chapter_path(len(chapters) + 1, title),
                title=title,
                html=body,
                start_page=start,
                end_page=end,
            )
        )
    if not chapters:
        raise BookError("PDF without extractable text.")
    note = f"{how} ({len(chapters)} chapters, {page_count} pages)"
    if failed:
        note += f"; {failed} pages without readable text"
    return ExtractedBook("pdf", meta, tuple(chapters), note, pages=page_count)


def _pdf_meta(reader: Any) -> BookMeta:
    info = reader.metadata
    if info is None:
        return BookMeta()

    def field_(name: str) -> str:
        value = getattr(info, name, None)
        return _SPACES.sub(" ", str(value)).strip() if value else ""

    author = field_("author")
    return BookMeta(
        title=field_("title"),
        authors=tuple(a.strip() for a in re.split(r"[;&]| and ", author) if a.strip()),
        description=field_("subject"),
        publisher=field_("producer") or None,
    )


def _outline_entries(reader: Any, outline: list[Any], depth: int = 1) -> list[tuple[int, str, int]]:
    entries: list[tuple[int, str, int]] = []
    for item in outline:
        if isinstance(item, list):
            entries.extend(_outline_entries(reader, item, depth + 1))
            continue
        try:
            page = reader.get_destination_page_number(item)
        except Exception:
            continue
        title = _SPACES.sub(" ", str(getattr(item, "title", "") or "")).strip()
        if title and page is not None and page >= 0:
            entries.append((depth, title, page + 1))
    return entries


def _pdf_ranges(reader: Any, pages: list[str]) -> tuple[list[tuple[str, int, int]], str]:
    count = len(pages)
    try:
        entries = _outline_entries(reader, list(reader.outline))
    except Exception:
        entries = []
    for depth in (1, 2):
        starts: list[tuple[str, int]] = []
        for entry_depth, title, page in entries:
            if entry_depth <= depth and (not starts or page > starts[-1][1]) and page <= count:
                starts.append((title, page))
        if len(starts) >= 2:
            return _ranges_from_starts(starts, pages), f"PDF bookmarks (level {depth})"
    headings = _heading_starts(pages)
    if len(headings) >= 2:
        return _ranges_from_starts(headings, pages), '"Chapter N" headings at the top of pages'
    windows = [
        (f"Páginas {a}–{min(a + PDF_WINDOW - 1, count)}", a)
        for a in range(1, count + 1, PDF_WINDOW)
    ]
    return _ranges_from_starts(
        windows, pages
    ), f"windows of {PDF_WINDOW} pages (the PDF has no structure)"


def _heading_starts(pages: list[str]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for number, text in enumerate(pages, start=1):
        for line in [ln.strip() for ln in text.splitlines() if ln.strip()][:3]:
            if _CHAPTER_HEADING.match(line):
                found.append((line[:120], number))
                break
    return found


def _ranges_from_starts(
    starts: list[tuple[str, int]], pages: list[str]
) -> list[tuple[str, int, int]]:
    count = len(pages)
    ranges: list[tuple[str, int, int]] = []
    if starts[0][1] > 1:
        front = "\n".join(pages[: starts[0][1] - 1])
        if len(front.strip()) >= MIN_FRONT_MATTER_CHARS:
            ranges.append(("Início", 1, starts[0][1] - 1))
        else:
            starts = [(starts[0][0], 1), *starts[1:]]  # a folha de rosto vai para o 1.º capítulo
    for i, (title, start) in enumerate(starts):
        end = starts[i + 1][1] - 1 if i + 1 < len(starts) else count
        ranges.append((title, start, max(end, start)))
    return ranges


_SENTENCE_END = re.compile(r"[.!?:;…»”\"')\]]$")


def _pdf_paragraphs(pages: list[str]) -> list[str]:
    """Parágrafos do texto das páginas. O pypdf devolve linhas; juntam-se as linhas de um
    parágrafo (e as palavras partidas com hífen) e separam-se os parágrafos pelas linhas em
    branco e pelas linhas curtas que acabam uma frase. Um parágrafo que continua na página
    seguinte é emendado."""
    paragraphs: list[str] = []
    carry = False  # o último parágrafo da página anterior ficou a meio
    for page in pages:
        lines = [ln.rstrip() for ln in page.splitlines()]
        widths = [len(ln.strip()) for ln in lines if ln.strip()]
        widest = max(widths, default=0)
        current: list[str] = []
        page_paragraphs: list[str] = []

        def close(current: list[str] = current, out: list[str] = page_paragraphs) -> None:
            if current:
                out.append(_join_lines(current))
                current.clear()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                close()
                continue
            if re.fullmatch(r"\d{1,4}", stripped):  # número de página isolado
                continue
            current.append(stripped)
            if widest and len(stripped) < 0.6 * widest and _SENTENCE_END.search(stripped):
                close()
        close()
        if not page_paragraphs:
            continue
        if carry and paragraphs and not page_paragraphs[0][:1].isupper():
            paragraphs[-1] = _join_lines([paragraphs[-1], page_paragraphs.pop(0)])
        paragraphs.extend(page_paragraphs)
        carry = bool(paragraphs) and not _SENTENCE_END.search(paragraphs[-1])
    return [p for p in paragraphs if p.strip()]


def _join_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        if out.endswith("-") and out[-2:-1].isalpha() and line[:1].islower():
            out = out[:-1] + line
        else:
            out = f"{out} {line}" if out else line
    return _CONTROL.sub("", _SPACES.sub(" ", out)).strip()


# --- entrada única -------------------------------------------------------------------------------


def extract_book(path: Path) -> ExtractedBook:
    """Extrai um EPUB ou PDF por capítulos (o MOBI converte-se antes, no Calibre)."""
    fmt = sniff_format(path)
    if fmt == "epub":
        return extract_epub(path)
    if fmt == "pdf":
        return extract_pdf(path)
    raise BookError("MOBI: convert it to EPUB first (Calibre does so when adding).")


# --- versão do extrator e extração isolada -------------------------------------------------------


@cache
def extractor_fingerprint() -> str:
    """Identifica **tudo** o que decide o texto e as posições de um capítulo: a versão do extrator,
    as do bs4 e do pypdf e o código do saneamento do HTML e da extração de blocos. Se mudar, o
    `books sync` volta a extrair (e as citações antigas deixam de valer para o texto novo)."""
    import bs4
    import pypdf

    from ariadne.courses import render
    from ariadne.sources import zim

    parts: list[str] = [str(EXTRACTOR_VERSION), bs4.__version__, pypdf.__version__]
    for name in ("ALLOWED_TAGS", "DROP_WITH_CONTENT", "ALLOWED_ATTRS", "SAFE_SCHEMES"):
        parts.append(repr(sorted(map(str, getattr(render, name)))))
    for obj in (
        render.sanitize_html,
        render._emit,
        render._attributes,
        zim.extract_blocks,
        zim.make_sections,
        zim.content_root,
        zim._Extractor,
        hidden_texts,
        _clean_chapter_html,
        _refine_chapters,
        _split_chapter,
        _pdf_paragraphs,
    ):
        try:
            parts.append(inspect.getsource(obj))
        except (OSError, TypeError):  # sem o código-fonte (só bytecode): usa-se o bytecode
            code = getattr(obj, "__code__", None)
            parts.append(repr(code.co_code) if code is not None else repr(obj))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def book_to_json(book: ExtractedBook) -> dict[str, Any]:
    return dataclasses.asdict(book)


def book_from_json(data: dict[str, Any]) -> ExtractedBook:
    meta = data["meta"]
    return ExtractedBook(
        format=data["format"],
        meta=BookMeta(
            title=meta["title"],
            authors=tuple(meta["authors"]),
            language=meta["language"],
            publisher=meta["publisher"],
            published=meta["published"],
            description=meta["description"],
            identifiers=dict(meta["identifiers"]),
        ),
        chapters=tuple(
            ExtractedChapter(**{**c, "hidden": tuple(c.get("hidden", ()))})
            for c in data["chapters"]
        ),
        note=data["note"],
        pages=data["pages"],
    )


def child_main(argv: list[str]) -> int:
    """O que corre no subprocesso (`python -I -m ariadne.books.isolated check|extract FICHEIRO`):
    deteta o formato, recusa DRM e (em `extract`) extrai; responde em JSON no stdout."""
    command, path = argv[0], Path(argv[1])
    try:
        fmt = sniff_format(path)
        check_drm(path, fmt)
        if command == "check":
            result: dict[str, Any] = {"format": fmt}
        else:
            result = book_to_json(extract_book(path))
    except DrmError as exc:
        sys.stdout.write(json.dumps({"error": str(exc), "drm": True}))
        return 4
    except BookError as exc:
        sys.stdout.write(json.dumps({"error": str(exc)}))
        return 3
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


def _isolated(command: str, path: Path) -> dict[str, Any]:
    """Corre `child_main` num subprocesso com limites de memória virtual (`ulimit -v`), de tempo
    de CPU (`ulimit -t`) e de tempo de relógio: um ficheiro hostil (zip, XML, HTML ou PDF
    patológicos) mata o filho, não o server nem o worker. Onde o `ulimit -v` não existe (macOS) fica
    o limite de tempo."""
    script = (
        f"ulimit -v {EXTRACT_MEMORY_MB * 1024} 2>/dev/null; "
        f"ulimit -t {max(1, math.ceil(EXTRACT_TIMEOUT))} 2>/dev/null; "
        'exec "$0" "$@"'
    )
    argv = ["/bin/sh", "-c", script, sys.executable, "-I", "-m", "ariadne.books.isolated"]
    try:
        done = subprocess.run(
            [*argv, command, str(path)],
            capture_output=True,
            timeout=EXTRACT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BookError(
            f"Reading the book took more than {EXTRACT_TIMEOUT:g} s and was interrupted."
        ) from exc
    try:
        payload = json.loads(done.stdout.decode("utf-8", "replace") or "null")
    except ValueError:
        payload = None
    if done.returncode == 0 and isinstance(payload, dict):
        return payload
    if done.returncode in (3, 4) and isinstance(payload, dict):
        message = str(payload.get("error", "unreadable file"))
        raise (DrmError if payload.get("drm") else BookError)(message)
    raise BookError(
        f"Reading the book failed or exceeded the limits ({EXTRACT_MEMORY_MB} MB of memory, "
        f"{EXTRACT_TIMEOUT:g} s): the file is too heavy or corrupt."
    )


def check_isolated(path: Path) -> str:
    """O formato do ficheiro (`epub`, `pdf` ou `mobi`), depois de recusado o DRM; tudo num
    subprocesso com limites."""
    return str(_isolated("check", path)["format"])


def extract_book_isolated(path: Path) -> ExtractedBook:
    """`extract_book` num subprocesso com limites de memória e de tempo (também para o pypdf)."""
    return book_from_json(_isolated("extract", path))
