"""Um servidor HTTP local que imita o catálogo OPDS do Kiwix, os `.meta4` e os espelhos de ZIM.

Nunca se toca na internet nos testes: o catálogo, os `.meta4` e os ficheiros vêm daqui.
"""

import hashlib
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tests.zimfactory import make_zim

FEED = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" '
    'xmlns:opds="https://specs.opds.io/opds-1.2" '
    'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
    'xmlns:thr="http://purl.org/syndication/thread/1.0">\n'
    "<id>feed</id><title>t</title>\n{head}{body}</feed>"
)


@dataclass
class Book:
    id: str
    name: str
    title: str
    language: str
    category: str
    flavour: str
    date: str  # AAAA-MM
    data: bytes
    summary: str = "um livro de teste"
    advertise_sha: bool = True
    fail_first_bytes: int = 0  # o 1.º pedido corta a ligação depois destes bytes
    filename: str = ""
    served: int = 0
    ranges: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.filename = self.filename or f"{self.name}_{self.flavour}_{self.date}.zim"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class FakeKiwixCatalog:
    def __init__(self) -> None:
        self.books: dict[str, Book] = {}
        self.requests: list[str] = []
        self.redirect_to: str | None = None  # os espelhos redirecionam para aqui
        self.port = 0
        self.mirror_status = 200

    @property
    def base(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def entries_url(self) -> str:
        return f"{self.base}/catalog/v2/entries"

    def add(self, book: Book) -> Book:
        self.books[book.id] = book
        return book

    # --- XML ---

    def entry_xml(self, book: Book) -> str:
        return (
            f"<entry><id>urn:uuid:{book.id}</id><title>{escape(book.title)}</title>"
            f"<updated>{book.date}-01T00:00:00Z</updated><summary>{escape(book.summary)}</summary>"
            f"<language>{book.language}</language><name>{book.name}</name>"
            f"<flavour>{book.flavour}</flavour><category>{book.category}</category>"
            "<tags>wikipedia;_category:wikipedia;_pictures:yes</tags>"
            "<articleCount>3</articleCount><mediaCount>1</mediaCount>"
            '<link rel="http://opds-spec.org/image/thumbnail" '
            f'href="/catalog/v2/illustration/{book.id}/" type="image/png"/>'
            '<link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim" '
            f'href="{self.base}/zim/{book.filename}.meta4" length="{len(book.data) + 61}" />'
            "</entry>"
        )

    def meta4(self, book: Book) -> str:
        sha = f'<hash type="sha-256">{book.sha256}</hash>' if book.advertise_sha else ""
        return (
            '<?xml version="1.0"?><metalink xmlns="urn:ietf:params:xml:ns:metalink">'
            f'<file name="{book.filename}"><size>{len(book.data)}</size>{sha}'
            f'<url priority="1">{self.base}/mirror/{book.filename}</url>'
            f'<url priority="2">{self.base}/mirror2/{book.filename}</url></file></metalink>'
        )


def make_book(
    directory: Path, book_id: str, name: str, *, language: str = "por", **kwargs: object
) -> Book:
    """Um livro do catálogo falso cujos bytes são um ZIM válido (com o `Name`/`Flavour` certos)."""
    flavour = str(kwargs.pop("flavour", "maxi"))
    date = str(kwargs.pop("date", "2026-09"))
    title = str(kwargs.pop("title", name.replace("_", " ")))
    category = str(kwargs.pop("category", "wikipedia"))
    path = directory / f"{name}_{flavour}_{date}.zim"
    make_zim(path, name=name, title=title, language=language, date=f"{date}-15", flavour=flavour)
    data = path.read_bytes()
    path.unlink()
    return Book(
        id=book_id,
        name=name,
        title=title,
        language=language,
        category=category,
        flavour=flavour,
        date=date,
        data=data,
        **kwargs,  # type: ignore[arg-type]
    )


def _handler(fake: FakeKiwixCatalog) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(
            self, code: int, body: bytes, ctype: str = "application/xml", **extra: str
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for key, value in extra.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            fake.requests.append(self.path)
            path = url.path
            if path == "/catalog/v2/entries":
                found = [
                    b
                    for b in fake.books.values()
                    if (not query.get("q") or query["q"].lower() in (b.title + b.summary).lower())
                    and (not query.get("lang") or query["lang"] in b.language.split(","))
                    and (not query.get("category") or query["category"] == b.category)
                ]
                start, count = int(query.get("start", 0)), int(query.get("count", 10))
                page = found[start : start + count]
                head = (
                    f"<opensearch:totalResults>{len(found)}</opensearch:totalResults>"
                    f"<opensearch:startIndex>{start}</opensearch:startIndex>"
                )
                self._send(
                    200,
                    FEED.format(head=head, body="".join(fake.entry_xml(b) for b in page)).encode(),
                )
            elif m := re.fullmatch(r"/catalog/v2/entry/([0-9a-f-]+)", path):
                book = fake.books.get(m.group(1))
                if book is None:
                    self._send(404, b"nao existe")
                else:  # como o real: um <entry> solto, sem declaração de namespaces
                    self._send(200, ('<?xml version="1.0"?>\n' + fake.entry_xml(book)).encode())
            elif path == "/catalog/v2/languages":
                langs = sorted({b.language for b in fake.books.values()})
                body = "".join(
                    f"<entry><title>{lang}</title><dc:language>{lang}</dc:language>"
                    "<thr:count>1</thr:count></entry>"
                    for lang in langs
                )
                self._send(200, FEED.format(head="", body=body).encode())
            elif path == "/catalog/v2/categories":
                cats = sorted({b.category for b in fake.books.values()})
                body = "".join(f"<entry><title>{c}</title></entry>" for c in cats)
                self._send(200, FEED.format(head="", body=body).encode())
            elif m := re.fullmatch(r"/zim/(.+)\.meta4", path):
                book = next((b for b in fake.books.values() if b.filename == m.group(1)), None)
                self._send(200, fake.meta4(book).encode()) if book else self._send(404, b"x")
            elif m := re.fullmatch(r"/mirror/(.+)", path):
                self._mirror(m.group(1))
            elif m := re.fullmatch(r"/mirror2/(.+)", path):
                self._mirror(m.group(1), second=True)
            elif path == "/redirected":
                self._send(200, b"x")
            else:
                self._send(404, b"nao existe")

        def _mirror(self, filename: str, second: bool = False) -> None:
            book = next((b for b in fake.books.values() if b.filename == filename), None)
            if book is None or fake.mirror_status != 200:
                self._send(fake.mirror_status if book else 404, b"x")
                return
            if fake.redirect_to is not None and not second:
                self.send_response(302)
                self.send_header("Location", fake.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = book.data
            start = 0
            code = 200
            header = self.headers.get("Range")
            if header:
                book.ranges.append(header)
                match = re.fullmatch(r"bytes=(\d+)-", header)
                if match:
                    start = int(match.group(1))
                    code = 206
                    if start >= len(data):
                        self._send(416, b"", "application/octet-stream")
                        return
            body = data[start:]
            self.send_response(code)
            self.send_header("Content-Type", "application/x-zim")
            self.send_header("Content-Length", str(len(body)))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.end_headers()
            if book.fail_first_bytes and book.served == 0:
                book.served += 1
                self.wfile.write(body[: book.fail_first_bytes])
                self.wfile.flush()
                self.connection.close()  # corta a ligação a meio
                return
            book.served += 1
            self.wfile.write(body)

    return Handler


@contextmanager
def serve(fake: FakeKiwixCatalog) -> Iterator[FakeKiwixCatalog]:
    server = ThreadingHTTPServer(("localhost", 0), _handler(fake))
    fake.port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


__all__ = ["Book", "FakeKiwixCatalog", "make_book", "serve"]
