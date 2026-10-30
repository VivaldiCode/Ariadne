"""Cliente do kiwix-serve: pesquisa de texto no nível 1 (ARCHITECTURE.md, secção 4.3).

Só a pesquisa passa por HTTP; a leitura dos artigos é direta, com libzim (`ariadne.sources`).
"""

import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote, unquote

import httpx

_CONTENT_LINK = re.compile(r"^/content/([^/]+)/(.+)$")
_NO_SUCH_BOOK = re.compile(r"No such book:\s*([^\s<]+)")
_SPACES = re.compile(r"\s+")
_ATOM = "{http://www.w3.org/2005/Atom}"
CATALOG_TTL_SECONDS = 300.0


class KiwixError(Exception):
    """O kiwix-serve respondeu mal ou não percebemos a resposta."""


class KiwixUnavailable(KiwixError):
    """Erro de rede: o kiwix-serve não responde."""


class KiwixBookMissing(KiwixError):
    """O kiwix-serve não conhece um dos livros (ZIM acrescentado depois de ele arrancar)."""

    def __init__(self, book: str) -> None:
        super().__init__(f"kiwix-serve does not know the book {book!r} (restart kiwix)")
        self.book = book


@dataclass(frozen=True)
class KiwixHit:
    book: str  # nome do livro no kiwix: o ficheiro ZIM sem '.zim'
    path: str  # caminho do artigo dentro do ZIM
    title: str
    snippet: str  # excerto em texto simples (sem <b>)


# Caracteres que o caminho de um URL admite sem '%': links como Ray_(film) ficam legíveis.
_PATH_SAFE = "/()'!*,;:@&=+$~"


def content_url(base_url: str, book: str, path: str) -> str:
    """URL de leitura humana de um artigo no kiwix-serve."""
    return f"{base_url.rstrip('/')}/content/{quote(book)}/{quote(path, safe=_PATH_SAFE)}"


class KiwixClient:
    def __init__(
        self, base_url: str, *, timeout: float = 10.0, client: httpx.Client | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._catalog: set[str] | None = None
        self._catalog_at = 0.0
        self._catalog_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def known_book_ids(self) -> set[str] | None:
        """UUIDs dos livros que o kiwix-serve tem na biblioteca (o seu catálogo OPDS), em cache
        por CATALOG_TTL_SECONDS. Serve para não pedir livros que ele ainda não conhece (ZIM novos na
        pasta antes de o kiwix os carregar): cada um custaria um pedido falhado. None = não se sabe
        (catálogo inacessível ou ilegível): quem chama pede tudo, como antes."""
        with self._catalog_lock:
            if (
                self._catalog is not None
                and time.monotonic() - self._catalog_at < CATALOG_TTL_SECONDS
            ):
                return self._catalog
        try:
            response = self._client.get(
                f"{self.base_url}/catalog/v2/entries", params={"count": "-1"}
            )
            root = ET.fromstring(response.text) if response.status_code == 200 else None
        except (httpx.HTTPError, ET.ParseError):
            root = None
        if root is None or root.tag != f"{_ATOM}feed":
            return None
        ids = {
            raw.removeprefix("urn:uuid:")
            for entry in root.iterfind(f"{_ATOM}entry")
            if (raw := (entry.findtext(f"{_ATOM}id") or "").strip())
        }
        with self._catalog_lock:
            self._catalog, self._catalog_at = ids, time.monotonic()
        return ids

    def search(
        self,
        query: str,
        books: Sequence[str] | None = None,
        k: int = 10,
        *,
        book_ids: Sequence[str] | None = None,
    ) -> list[KiwixHit]:
        """Pesquisa de texto completo, limitada a livros por nome (`books`) ou pelo UUID do ZIM
        (`book_ids`, o mais robusto: o nome depende de como o kiwix montou a biblioteca)."""
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("pattern", query),
            ("format", "xml"),
            ("pageLength", str(k)),
        ]
        params.extend(("books.name", book) for book in books or ())
        params.extend(("books.id", book_id) for book_id in book_ids or ())
        try:
            response = self._client.get(f"{self.base_url}/search", params=params)
        except httpx.HTTPError as exc:
            raise KiwixUnavailable(f"kiwix-serve unreachable: {type(exc).__name__}") from exc
        if response.status_code == 400 and (missing := _NO_SUCH_BOOK.search(response.text)):
            raise KiwixBookMissing(missing.group(1))
        if response.status_code != 200:
            raise KiwixError(f"kiwix-serve answered HTTP {response.status_code}")
        return parse_search(response.text)[:k]


def parse_search(xml: str) -> list[KiwixHit]:
    """RSS do /search?format=xml -> resultados, pela ordem do kiwix."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise KiwixError("unreadable kiwix-serve response") from exc
    hits: list[KiwixHit] = []
    for item in root.iterfind("./channel/item"):
        link = _CONTENT_LINK.match(item.findtext("link") or "")
        if link is None:
            continue
        hits.append(
            KiwixHit(
                book=unquote(link.group(1)),
                path=unquote(link.group(2)),
                title=(item.findtext("title") or "").strip(),
                snippet=_clean_snippet(item.find("description")),
            )
        )
    return hits


def _clean_snippet(description: ET.Element | None) -> str:
    # Os termos encontrados vêm em <b> dentro da descrição: itertext() devolve-os sem as marcas.
    raw = "".join(description.itertext()) if description is not None else ""
    text = _SPACES.sub(" ", raw).strip()
    # O kiwix marca os cortes com reticências de pontos: passam a '…'.
    text = re.sub(r"^\.{3,}\s*", "… ", text)
    return re.sub(r"\s*\.{3,}$", " …", text)
