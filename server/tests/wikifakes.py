"""Dobles da Fase 6: um Wiki.js em memória e um curador (servidor HTTP local compatível com a
OpenAI) cujo comportamento cada teste escolhe."""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ariadne.wiki.base import (
    WikiBackend,
    WikiError,
    WikiHit,
    WikiNotFound,
    WikiPage,
    WikiUnavailable,
)


class FakeWiki(WikiBackend):
    """Wiki.js em memória: guarda as páginas por caminho e conta as escritas."""

    def __init__(self) -> None:
        self.pages: dict[str, WikiPage] = {}  # os artigos, por «locale/caminho»
        self.index_pages: dict[str, WikiPage] = {}  # as páginas de domínio (etiqueta `dominio`)
        self.index_writes: list[str] = []
        self.writes: list[str] = []  # um caminho por upsert
        self.down = False
        self.locales: frozenset[str] | None = None  # o que `active_locales` responde
        self.moves: list[tuple[str, str]] = []  # (de, para), «locale/caminho»
        self.locale_of: dict[str, str] = {}  # chave da página -> locale
        self.boom = False  # `search` rebenta com uma exceção que não é do Wiki.js
        self.hits: list[WikiHit] = []  # o que `search` devolve
        self.tags: dict[str, list[str]] = {}
        self.assets: dict[str, tuple[str, bytes]] = {}  # «pasta/nome» -> (tipo, bytes)
        self.uploads: list[str] = []  # um «pasta/nome» por envio
        self.fail_assets: set[str] = set()  # nomes de ficheiro cujo envio falha (WikiError)
        self._next = 100

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
        """As páginas guardam-se por `locale/caminho` (só `caminho` sem locale), a forma do URL."""
        if self.down:
            raise WikiUnavailable("Wiki.js em baixo")
        key = f"{locale}/{path}" if locale else path
        pages, writes = (
            (self.index_pages, self.index_writes)
            if "dominio" in tags
            else (self.pages, self.writes)
        )
        existing = pages.get(key)
        page_id = existing.page_id if existing else self._next
        if existing is None:
            self._next += 1
        pages[key] = WikiPage(page_id, path, title, content_md, description)
        self.tags[key] = tags
        writes.append(key)
        if locale:
            self.locale_of[key] = locale
        return page_id

    def move_page(self, page_id: int, path: str, locale: str) -> None:
        if self.down:
            raise WikiUnavailable("Wiki.js em baixo")
        pages = next(
            (
                group
                for group in (self.pages, self.index_pages)
                if any(p.page_id == page_id for p in group.values())
            ),
            None,
        )
        if pages is None:
            raise WikiNotFound(f"a página {page_id} não existe")
        old = next(k for k, p in pages.items() if p.page_id == page_id)
        new = f"{locale}/{path}"
        if new in self.pages or new in self.index_pages:
            raise WikiError(f"o destino {new} já existe")
        page = pages.pop(old)
        pages[new] = WikiPage(page.page_id, path, page.title, page.content, page.description)
        self.tags[new] = self.tags.pop(old, [])
        self.locale_of[new] = locale
        self.locale_of.pop(old, None)
        self.moves.append((old, new))

    def active_locales(self) -> frozenset[str] | None:
        return self.locales

    def get_page(self, page_id: int) -> WikiPage | None:
        everything = [*self.pages.values(), *self.index_pages.values()]
        return next((p for p in everything if p.page_id == page_id), None)

    def search(self, query: str, k: int, locale: str | None = None) -> list[WikiHit]:
        if self.boom:
            raise RuntimeError("erro inesperado")
        if self.down:
            raise WikiUnavailable("Wiki.js em baixo")
        return self.hits[:k]

    def upload_asset(self, folder: str, filename: str, data: bytes, mimetype: str) -> str:
        if self.down:
            raise WikiUnavailable("Wiki.js em baixo")
        if filename in self.fail_assets:
            raise WikiError(f"Wiki.js recusou {filename}")
        path = f"{folder}/{filename}"
        self.assets[path] = (mimetype, data)
        self.uploads.append(path)
        return f"/{path}"

    def asset_exists(self, path: str) -> bool:
        if self.down:
            raise WikiUnavailable("Wiki.js em baixo")
        return path in self.assets

    def health(self) -> str:
        return "ok"

    def close(self) -> None:
        pass


_ARTICLE = re.compile(
    r"<<<ARTICLE-(\w+)>>>\nTitle: [^\n]*\n\n?(.*)\n<<<END ARTICLE-\1>>>", re.DOTALL
)


class FakeCurator:
    """Servidor `/v1/chat/completions` local. `mode`: faithful | alter | reject | invalid | down."""

    def __init__(self, mode: str = "faithful") -> None:
        self.mode = mode
        self.requests: list[dict[str, Any]] = []
        self.delay = 0.0  # segundos até responder
        self.health_ok = True  # o /health do llama-server: False = «a carregar o modelo» (503)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                ok = outer.health_ok and self.path.split("?")[0] in ("/health", "/v1/models")
                self.send_response(200 if ok else 503 if self.path == "/health" else 404)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length))
                outer.requests.append(body)
                if outer.mode == "down":
                    self.send_response(503)
                    self.end_headers()
                    return
                if outer.delay:
                    time.sleep(outer.delay)
                reply = outer.reply(body)
                payload = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://localhost:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def reply(self, body: dict[str, Any]) -> str:
        user = body["messages"][-1]["content"]
        match = _ARTICLE.search(user)
        article = match.group(2) if match else ""
        if self.mode == "empty":
            return json.dumps({"verdict": "accept", "markdown": "", "issues": []})
        if self.mode == "short":
            return json.dumps({"verdict": "accept", "markdown": "Resumo.", "issues": []})
        if self.mode == "invalid":
            return "isto não é JSON"
        if self.mode == "reject":
            return json.dumps(
                {
                    "verdict": "reject",
                    "reason": "spam",
                    "issues": ["conteúdo malicioso"],
                    "markdown": "",
                }
            )
        lines = [
            line
            for line in article.splitlines()
            if "ignore all previous instructions" not in line.lower()
            and "curl" not in line
            and "<system>" not in line
        ]
        text = "\n".join(lines)
        if self.mode == "evil":  # deixa a injeção no texto e acrescenta HTML e um link
            text = article + "\n\n<img src=x onerror=alert(1)> [x](http://evil.example)"
        if self.mode == "tricks":  # limpa o texto mas acrescenta links, HTML e URLs de fora
            text += (
                "\n\n<scr<script>ipt>alert(1)</scr</script>ipt> <img src=x onerror=alert(1)>"
                "\n[clica](http://evil.example) [js](javascript:alert(1)) [rel](//evil.example)"
                "\n[ref][1] ![pixel](http://evil.example/p.png) http://evil.example/solto"
                "\n\n[1]: http://evil.example/ref"
            )
        if self.mode == "alter":
            text = text.replace("500", "5000")
        return (
            "```json\n"
            + json.dumps(
                {
                    "verdict": "accept",
                    "reason": "ok",
                    "issues": ["instruções escondidas removidas"],
                    "markdown": "Resumo de referência.\n\n" + text,
                }
            )
            + "\n```"
        )
