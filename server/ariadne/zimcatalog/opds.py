"""Cliente do catálogo OPDS do Kiwix (`KIWIX_CATALOG_URL`).

O catálogo é um feed Atom/OPDS 1.x (o «v2» é o caminho da API): `entries` com filtros `q`, `lang`
(ISO 639-3), `category`, `start` e `count`, `entry/<id>` para uma entrada, `categories` e
`languages`. Cada entrada traz um `.meta4` (Metalink) com o tamanho exato, o sha-256 e os
espelhos; o `length` do catálogo é só uma aproximação. A resposta guarda-se em cache pouco tempo.
"""

import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel

MAX_FEED_BYTES = 32 * 1024 * 1024
MAX_PAGE = 200
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
    "thr": "http://purl.org/syndication/thread/1.0",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
_WRAP = (
    '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" '
    'xmlns:opds="https://specs.opds.io/opds-1.2" xmlns:thr="http://purl.org/syndication/thread/1.0"'
    ' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
)
_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_ACQUISITION = "http://opds-spec.org/acquisition"


class CatalogError(Exception):
    """O catálogo não respondeu ou respondeu algo que não percebemos."""


class EntryNotFound(CatalogError):
    pass


class CatalogEntry(BaseModel):
    id: str  # o uuid do livro no catálogo (sem `urn:uuid:`)
    name: str  # o nome do livro (ex.: wikipedia_pt_all); duas variantes partilham-no
    title: str
    summary: str = ""
    language: str = ""  # ISO 639-3 separados por vírgulas (ex.: «eng,jpn»)
    category: str = ""
    flavour: str = ""  # maxi | nopic | mini | ...
    tags: list[str] = []
    article_count: int | None = None
    media_count: int | None = None
    size_bytes: int | None = None  # aproximado; o exato vem do .meta4
    updated: str = ""  # AAAA-MM-DD
    download_url: str | None = None  # o .meta4 (ou o próprio .zim)
    stem: str | None = None  # o nome do ficheiro sem `.zim` (= o nome do livro no kiwix-serve)


class CatalogPage(BaseModel):
    total: int
    start: int
    count: int
    items: list[CatalogEntry]


class FilterOption(BaseModel):
    code: str
    title: str
    count: int | None = None


class CatalogFilters(BaseModel):
    languages: list[FilterOption]
    categories: list[FilterOption]


def _parse_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_FEED_BYTES:
        raise CatalogError("the catalog response is too large")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise CatalogError("the catalog carries a DTD/entity declaration (refused)")
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text).lstrip()
    if not text.startswith("<feed"):  # `entry/<id>` devolve um <entry> solto, sem namespaces
        text = f"{_WRAP}{text}</feed>"
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise CatalogError(f"the catalog returned invalid XML ({exc})") from exc


def _text(node: ET.Element, tag: str) -> str:
    found = node.find(tag, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def _int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _entry(node: ET.Element, base: str) -> CatalogEntry | None:
    raw_id = _text(node, "a:id").removeprefix("urn:uuid:")
    name = _text(node, "a:name")
    if not _UUID.match(raw_id) or not name:
        return None
    download: str | None = None
    size: int | None = None
    for link in node.findall("a:link", NS):
        rel = link.get("rel") or ""
        if rel.startswith(_ACQUISITION) and link.get("href"):
            download = urljoin(base, link.get("href", ""))
            size = _int(link.get("length", ""))
            break
    stem = None
    if download:
        last = urlparse(download).path.rsplit("/", 1)[-1]
        last = last.removesuffix(".meta4")
        stem = last.removesuffix(".zim") if last.endswith(".zim") else None
    tags = [t for t in _text(node, "a:tags").split(";") if t and not t.startswith("_")]
    return CatalogEntry(
        id=raw_id,
        name=name,
        title=_text(node, "a:title") or name,
        summary=_text(node, "a:summary"),
        language=_text(node, "a:language"),
        category=_text(node, "a:category"),
        flavour=_text(node, "a:flavour"),
        tags=tags,
        article_count=_int(_text(node, "a:articleCount")),
        media_count=_int(_text(node, "a:mediaCount")),
        size_bytes=size,
        updated=_text(node, "a:updated")[:10],
        download_url=download,
        stem=stem,
    )


def parse_entries(data: bytes, base: str) -> CatalogPage:
    root = _parse_xml(data)
    items = [e for node in root.findall("a:entry", NS) if (e := _entry(node, base)) is not None]
    total = _int(_text(root, "os:totalResults") or _text(root, "a:totalResults"))
    start = _int(_text(root, "os:startIndex") or _text(root, "a:startIndex"))
    return CatalogPage(
        total=total if total is not None else len(items),
        start=start or 0,
        count=len(items),
        items=items,
    )


def parse_options(data: bytes, kind: str) -> list[FilterOption]:
    root = _parse_xml(data)
    options: list[FilterOption] = []
    for node in root.findall("a:entry", NS):
        title = _text(node, "a:title")
        code = _text(node, "dc:language") if kind == "language" else title
        count = _int(_text(node, "thr:count"))
        if code and title:
            options.append(FilterOption(code=code, title=title, count=count))
    return options


@dataclass
class CatalogClient:
    """Fala com o catálogo; as respostas guardam-se `ttl` segundos (a mesma página não se pede
    duas vezes seguidas). Seguro para vários fios."""

    entries_url: str
    ttl: float = 300.0
    timeout: float = 30.0
    client: httpx.Client | None = None
    _cache: dict[str, tuple[float, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout, follow_redirects=True)

    @property
    def root(self) -> str:
        """The root of the catalog API (`…/catalog/v2`)."""
        return self.entries_url.rstrip("/").removesuffix("/entries")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def _get(self, url: str, params: dict[str, str | int] | None = None) -> bytes:
        assert self.client is not None
        try:
            response = self.client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise CatalogError(f"catalog unreachable ({type(exc).__name__})") from exc
        if response.status_code == 404:
            raise EntryNotFound("does not exist in the catalog")
        if response.status_code >= 400:
            raise CatalogError(f"the catalog responded {response.status_code}")
        return response.content

    def _cached(self, key: str, build: Any) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1]
        value = build()
        with self._lock:
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[key] = (now, value)
        return value

    def search(
        self,
        q: str | None = None,
        lang: str | None = None,
        category: str | None = None,
        start: int = 0,
        count: int = 24,
    ) -> CatalogPage:
        count = max(1, min(count, MAX_PAGE))
        params: dict[str, str | int] = {"start": max(0, start), "count": count}
        if q:
            params["q"] = q
        if lang:
            params["lang"] = lang
        if category:
            params["category"] = category
        key = "entries?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))

        def build() -> CatalogPage:
            url = self.entries_url
            return parse_entries(self._get(url, params), url)

        page: CatalogPage = self._cached(key, build)
        return page

    def entry(self, book_id: str) -> CatalogEntry:
        if not _UUID.match(book_id):
            raise EntryNotFound("invalid identifier")
        url = f"{self.root}/entry/{book_id}"

        def build() -> CatalogEntry:
            page = parse_entries(self._get(url), url)
            if not page.items:
                raise EntryNotFound("does not exist in the catalog")
            return page.items[0]

        entry: CatalogEntry = self._cached(f"entry:{book_id}", build)
        return entry

    def filters(self) -> CatalogFilters:
        def build() -> CatalogFilters:
            return CatalogFilters(
                languages=parse_options(self._get(f"{self.root}/languages"), "language"),
                categories=parse_options(self._get(f"{self.root}/categories"), "category"),
            )

        value: CatalogFilters = self._cached("filters", build)
        return value
