#!/usr/bin/env python3
"""Agente HTTP do Calibre: a porta de entrada do `calibredb` e do `ebook-convert` para o Ariadne.

O Calibre é a base canónica da biblioteca de livros (STRUCTURE.md, secção 6.3) e só se usa pela
linha de comandos. Em vez de pôr o Calibre (Qt, centenas de MB) na imagem do server, este
contentor guarda-o e expõe o mínimo por HTTP, com chave de API: adicionar um livro, ler os
metadados, converter para outro formato, remover. Só biblioteca estândar de Python (corre com o
Python da imagem, não com o do Calibre).

    POST   /books?filename=x.epub        corpo = o ficheiro        -> o livro (JSON)
    GET    /books                                                  -> lista de livros
    GET    /books/{id}                                             -> um livro
    POST   /books/{id}/convert?to=epub                             -> o livro, com o formato novo
    DELETE /books/{id}                                             -> {"removed": id}
    GET    /healthz                                                -> {"status": "ok", ...} (sem chave)

Os caminhos dos formatos são relativos à biblioteca (`/library`), a mesma pasta que o Kavita lê.
"""

import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LIBRARY = Path(os.environ.get("CALIBRE_LIBRARY", "/library"))
API_KEY = os.environ.get("CALIBRE_API_KEY", "")
MAX_BYTES = int(os.environ.get("CALIBRE_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
COMMAND_TIMEOUT = int(os.environ.get("CALIBRE_COMMAND_TIMEOUT", "900"))
ALLOWED_INPUT = {"epub", "pdf", "mobi", "azw3"}
ALLOWED_OUTPUT = {"epub", "pdf"}
FIELDS = "id,title,authors,formats,identifiers,languages,publisher,pubdate,comments,timestamp,tags"
# O calibredb trava a base de dados: um comando de cada vez.
LOCK = threading.Lock()
VERSION = ""  # lida uma vez no arranque: o /healthz não corre nada nem espera pelo LOCK
_ID = re.compile(r"^[0-9]{1,9}$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class CalibreError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def run(args: list[str], timeout: int = COMMAND_TIMEOUT) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - argumentos fixos, nunca uma shell
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CalibreError(f"{args[0]} demorou mais de {timeout} s") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["sem detalhe"]
        raise CalibreError(f"{Path(args[0]).name} falhou: {detail[0][:300]}")
    return done.stdout


def calibredb(*args: str) -> str:
    return run(["calibredb", *args, "--library-path", str(LIBRARY)])


def ensure_library() -> None:
    """Uma biblioteca nova: o `calibredb` só cria a base de dados ao adicionar o 1.º livro, por
    isso cria-se aqui com um livro vazio que logo se remove."""
    if (LIBRARY / "metadata.db").exists():
        return
    LIBRARY.mkdir(parents=True, exist_ok=True)
    out = calibredb("add", "--empty")
    match = re.search(r"ids?: *([0-9]+)", out)
    if match:
        calibredb("remove", match.group(1), "--permanent")


def describe(raw: dict) -> dict:
    formats = {}
    for path in raw.get("formats") or []:
        relative = os.path.relpath(path, LIBRARY)
        if not relative.startswith(".."):
            formats[Path(path).suffix.lstrip(".").upper()] = relative
    languages = raw.get("languages") or []
    return {
        "id": raw["id"],
        "title": raw.get("title") or "",
        "authors": [a for a in re.split(r"\s*&\s*", raw.get("authors") or "") if a],
        "language": languages[0] if languages else None,
        "publisher": raw.get("publisher") or None,
        # Sem data o Calibre escreve 0101-01-01.
        "published": None
        if (raw.get("pubdate") or "").startswith("0101")
        else ((raw.get("pubdate") or "")[:10] or None),
        "description": raw.get("comments") or "",
        "identifiers": raw.get("identifiers") or {},
        "tags": raw.get("tags") or [],
        "formats": formats,
        "added": raw.get("timestamp"),
    }


def list_books(search: str | None = None) -> list[dict]:
    args = ["list", "--for-machine", "--fields", FIELDS, "--limit", "100000"]
    if search:
        args += ["--search", search]
    out = calibredb(*args).strip()
    return [describe(item) for item in json.loads(out)] if out else []


def get_book(book_id: str) -> dict:
    found = list_books(f"id:{book_id}")
    if not found:
        raise CalibreError(f"livro {book_id} não existe", 404)
    return found[0]


def add_book(filename: str, body_path: Path, title: str = "", authors: str = "") -> dict:
    extension = Path(filename).suffix.lstrip(".").lower()
    if extension not in ALLOWED_INPUT:
        raise CalibreError(f"formato não aceite: {extension or '?'}", 400)
    options = []
    if title:
        options += ["--title", title[:300]]
    if authors:
        options += ["--authors", authors[:300]]  # vários autores separados por «&»
    out = calibredb("add", "--duplicates", *options, str(body_path))
    match = re.search(r"ids?: *([0-9]+)", out)
    if not match:
        raise CalibreError(f"o Calibre não adicionou o livro: {out.strip()[:200]}")
    return get_book(match.group(1))


def convert(book_id: str, target: str) -> dict:
    if target not in ALLOWED_OUTPUT:
        raise CalibreError(f"formato de saída não aceite: {target}", 400)
    book = get_book(book_id)
    if target.upper() in book["formats"]:
        return book
    source = next(
        (LIBRARY / p for f, p in book["formats"].items() if f.lower() in ALLOWED_INPUT), None
    )
    if source is None:
        raise CalibreError("o livro não tem um formato de partida", 400)
    work = Path(tempfile.mkdtemp(prefix="convert-"))
    try:
        result = work / f"book.{target}"
        run(["ebook-convert", str(source), str(result)])
        calibredb("add_format", str(book_id), str(result))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return get_book(book_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "ariadne-calibre"
    protocol_version = "HTTP/1.1"
    # Um cliente que abre a ligação e não diz nada (ou manda o corpo a conta-gotas) não prende
    # uma thread para sempre: cada leitura do socket tem este limite.
    timeout = 30

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _send(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        key = self.headers.get("X-API-KEY", "")
        return bool(API_KEY) and hmac.compare_digest(key.encode(), API_KEY.encode())

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        try:
            if method == "GET" and parts == ["healthz"]:
                self._send(200, {"status": "ok", "calibre": VERSION})
                return
            if not self._authorized():
                self._drain()
                self._send(401, {"detail": "chave de API em falta ou errada"})
                return
            if method == "POST" and parts == ["books"]:
                self._upload(query)  # o corpo lê-se antes do LOCK: um upload lento não bloqueia
                return
            with LOCK:
                self._route(method, parts, query)
        except CalibreError as exc:
            self._send(exc.status, {"detail": str(exc)})
        except Exception as exc:  # o agente nunca cai por causa de um pedido
            self._send(500, {"detail": f"erro interno: {type(exc).__name__}"})

    def _drain(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        while length > 0:
            length -= len(self.rfile.read(min(length, 1 << 20)))

    def _route(self, method: str, parts: list[str], query: dict[str, list[str]]) -> None:
        if method == "GET" and parts == ["books"]:
            ensure_library()
            self._send(200, list_books())
        elif method == "GET" and len(parts) == 2 and parts[0] == "books" and _ID.match(parts[1]):
            ensure_library()
            self._send(200, get_book(parts[1]))
        elif (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "books"
            and _ID.match(parts[1])
            and parts[2] == "convert"
        ):
            ensure_library()
            self._send(200, convert(parts[1], (query.get("to") or [""])[0].lower()))
        elif method == "DELETE" and len(parts) == 2 and parts[0] == "books" and _ID.match(parts[1]):
            ensure_library()
            calibredb("remove", parts[1], "--permanent")
            self._send(200, {"removed": int(parts[1])})
        else:
            self._drain()
            self._send(404, {"detail": "rota desconhecida"})

    def _upload(self, query: dict[str, list[str]]) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            self._drain()
            raise CalibreError(f"tamanho inválido ({length} bytes; máximo {MAX_BYTES})", 413)
        name = _SAFE.sub("_", (query.get("filename") or ["livro.epub"])[0]) or "livro.epub"
        if len(name) > 120:  # corta o nome, não a extensão (senão o formato deixa de ser conhecido)
            stem, dot, extension = name.rpartition(".")
            name = (stem[:110] + dot + extension) if dot and len(extension) <= 8 else name[:120]
        work = Path(tempfile.mkdtemp(prefix="upload-"))
        try:
            target = work / name
            with target.open("wb") as handle:
                remaining = length
                while remaining > 0:
                    block = self.rfile.read(min(remaining, 1 << 20))
                    if not block:
                        raise CalibreError("corpo do pedido incompleto", 400)
                    handle.write(block)
                    remaining -= len(block)
            title = (query.get("title") or [""])[0]
            authors = (query.get("authors") or [""])[0]
            with LOCK:  # só o `calibredb` é um de cada vez
                ensure_library()
                result = add_book(name, target, title, authors)
            self._send(200, result)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")


def main() -> None:
    if not API_KEY:
        sys.exit("CALIBRE_API_KEY não está definida: o agente recusa-se a arrancar sem chave.")
    port = int(os.environ.get("CALIBRE_AGENT_PORT", "8080"))
    global VERSION
    with LOCK:
        ensure_library()
        VERSION = run(["calibredb", "--version"], 30).strip()
    ThreadingHTTPServer(("", port), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
