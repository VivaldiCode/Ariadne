"""`WikiBackend` para o Wiki.js 2.5 (API GraphQL em `/graphql`, chave de API no Bearer).

Locale: cada página leva o seu (a língua dela, `docs/WIKI-IMPORT.md` secção 5), com o
*namespacing* do Wiki.js ligado: o URL fica `/<locale>/<caminho>`. `WIKIJS_LOCALE` (`en`) é o
locale por omissão, para quem não diz qual (as pesquisas e o `upsert_page` sem `locale`).
"""

import json
import threading
import time
from typing import Any

import httpx

from ariadne.wiki.base import (
    WikiBackend,
    WikiError,
    WikiHit,
    WikiNotFound,
    WikiPage,
    WikiUnavailable,
)

SEARCH_TIMEOUT = 3.0  # a pesquisa corre em paralelo com o kiwix: o Wiki.js lento não a atrasa

_PAGE_BY_PATH = """query($path: String!, $locale: String!) {
  pages { singleByPath(path: $path, locale: $locale) { id } } }"""
_CREATE = """mutation($content: String!, $description: String!, $locale: String!, $path: String!,
  $tags: [String]!, $title: String!) {
  pages { create(content: $content, description: $description, editor: "markdown",
    isPublished: true, isPrivate: false, locale: $locale, path: $path, tags: $tags, title: $title) {
      responseResult { succeeded message errorCode } page { id } } } }"""
_UPDATE = """mutation($id: Int!, $content: String!, $description: String!, $tags: [String],
  $title: String!) {
  pages { update(id: $id, content: $content, description: $description, tags: $tags,
    title: $title, isPublished: true) {
      responseResult { succeeded message errorCode } page { id } } } }"""
_MOVE = """mutation($id: Int!, $path: String!, $locale: String!) {
  pages { move(id: $id, destinationPath: $path, destinationLocale: $locale) {
    responseResult { succeeded message errorCode } } } }"""
_LOCALIZATION = """{ localization { config { locale namespacing namespaces } } }"""
_SINGLE = """query($id: Int!) { pages { single(id: $id) {
  id path title content description } } }"""
_SEARCH = """query($query: String!) { pages { search(query: $query) {
  results { id title description path locale } } } }"""


_FOLDERS = """query($parent: Int!) { assets { folders(parentFolderId: $parent) { id slug } } }"""
_CREATE_FOLDER = """mutation($parent: Int!, $slug: String!) {
  assets { createFolder(parentFolderId: $parent, slug: $slug) {
    responseResult { succeeded message errorCode } } } }"""
UPLOAD_TIMEOUT = 60.0
LOCALES_TTL = 300.0  # os locales ativos mudam raramente: perguntam-se de 5 em 5 minutos


def check_health(url: str, token: str, client: httpx.Client | None = None) -> str:
    """`ok`, `ok (no token…)` ou o motivo de não responder, olhando para `/healthz`."""
    try:
        response = (client or httpx).get(f"{url.rstrip('/')}/healthz", timeout=2.0)
    except httpx.HTTPError as exc:
        return f"unavailable: {type(exc).__name__}"
    if response.status_code >= 500:
        return f"http {response.status_code}"
    return "ok" if token else "ok (no token: run `ariadne wiki bootstrap`)"


class WikiJsBackend(WikiBackend):
    def __init__(
        self,
        url: str,
        token: str,
        *,
        locale: str = "en",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.locale = locale
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._folder_ids: dict[str, int] = {}  # «ariadne/ab» -> id da pasta de assets
        self._folder_lock = threading.Lock()
        self._locales: tuple[float, frozenset[str] | None] | None = None  # (quando, locales)

    def close(self) -> None:
        self._client.close()

    def _call(
        self, query: str, variables: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                f"{self.url}/graphql",
                json={"query": query, "variables": variables},
                headers=self._headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:
            raise WikiUnavailable(f"Wiki.js unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise WikiUnavailable(f"Wiki.js answered HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise WikiError(f"Wiki.js answered HTTP {response.status_code} without JSON") from exc
        if body.get("errors"):
            message = "; ".join(str(e.get("message")) for e in body["errors"])[:300]
            if "does not exist" in message:  # o Wiki.js responde assim a uma página por criar
                raise WikiNotFound(message)
            raise WikiError(f"Wiki.js rejected the request: {message}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise WikiError(f"Wiki.js answered HTTP {response.status_code} without data")
        return data

    def _find(self, path: str, locale: str) -> int | None:
        try:
            data = self._call(_PAGE_BY_PATH, {"path": path, "locale": locale})
        except WikiNotFound:
            return None
        page = data["pages"]["singleByPath"]
        return int(page["id"]) if page else None

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
        locale = locale or self.locale
        page_id = self._find(path, locale)
        if page_id is None:
            data = self._call(
                _CREATE,
                {
                    "content": content_md,
                    "description": description,
                    "locale": locale,
                    "path": path,
                    "tags": tags,
                    "title": title,
                },
            )
            result = data["pages"]["create"]
            if result["responseResult"]["succeeded"]:
                return int(result["page"]["id"])
            page_id = self._find(path, locale)  # outro worker criou-a entretanto
            if page_id is None:
                raise WikiError(
                    f"Wiki.js did not create {path!r}: {result['responseResult']['message']}"
                )
        data = self._call(
            _UPDATE,
            {
                "id": page_id,
                "content": content_md,
                "description": description,
                "tags": tags,
                "title": title,
            },
        )
        result = data["pages"]["update"]
        if not result["responseResult"]["succeeded"]:
            raise WikiError(
                f"Wiki.js did not update {path!r}: {result['responseResult']['message']}"
            )
        return page_id

    def move_page(self, page_id: int, path: str, locale: str) -> None:
        data = self._call(_MOVE, {"id": page_id, "path": path, "locale": locale})
        result = data["pages"]["move"]["responseResult"]
        if not result["succeeded"]:
            raise WikiError(f"Wiki.js did not move page {page_id} to {locale}/{path}: "
                            f"{result['message']}")  # fmt: skip

    def active_locales(self) -> frozenset[str] | None:
        """Com o *namespacing* ligado, os locales dele mais o do site; desligado, só o do site.
        `None` se o Wiki.js não responde (e o resultado de uma pergunta falhada não se guarda)."""
        now = time.monotonic()
        cached = self._locales
        if cached is not None and now - cached[0] < LOCALES_TTL:
            return cached[1]
        try:
            config = self._call(_LOCALIZATION, {})["localization"]["config"]
        except (WikiError, KeyError, TypeError):
            return None
        locales = {str(config.get("locale") or self.locale)}
        if config.get("namespacing"):
            locales.update(str(n) for n in config.get("namespaces") or [])
        found = frozenset(locales)
        self._locales = (now, found)
        return found

    def get_page(self, page_id: int) -> WikiPage | None:
        try:
            data = self._call(_SINGLE, {"id": page_id})
        except WikiNotFound:
            return None
        page = data["pages"]["single"]
        if not page:
            return None
        return WikiPage(
            int(page["id"]),
            page["path"],
            page["title"],
            page["content"] or "",
            page["description"] or "",
        )

    def search(self, query: str, k: int, locale: str | None = None) -> list[WikiHit]:
        data = self._call(_SEARCH, {"query": query}, timeout=SEARCH_TIMEOUT)
        hits = [
            WikiHit(int(r["id"]), r["path"], r["title"], r["description"] or "")
            for r in data["pages"]["search"]["results"]
            if locale is None or r["locale"] == locale
        ]
        return hits[:k]

    # --- assets (imagens) --------------------------------------------------------------------
    # Descoberto contra o Wiki.js 2.5.314: as pastas só se criam e listam por GraphQL
    # (`createFolder` não devolve o id; lista-se a seguir); o envio é `POST /u` multipart com o
    # campo `mediaUpload` duas vezes, primeiro o JSON `{"folderId": N}` e depois o ficheiro.
    # Responde `200 ok`; sem chave, `403` com JSON; acima de 5 MB, `500` com uma página HTML.
    # O Wiki.js guarda o nome em minúsculas e troca os espaços por `_` (os nossos, hash +
    # extensão, já saem iguais), e o ficheiro serve-se em `/<pasta>/<nome>`. Enviar o mesmo nome
    # outra vez substitui o ficheiro sem erro.

    def _list_folders(self, parent: int) -> dict[str, int]:
        data = self._call(_FOLDERS, {"parent": parent})
        return {f["slug"]: int(f["id"]) for f in data["assets"]["folders"]}

    def _folder_id(self, folder: str) -> int:
        """O id da pasta de assets `a/b`, criando as que faltam (ids guardados em memória)."""
        with self._folder_lock:
            parent, walked = 0, ""
            for slug in folder.strip("/").split("/"):
                walked = f"{walked}/{slug}" if walked else slug
                if walked not in self._folder_ids:
                    children = self._list_folders(parent)
                    if slug not in children:
                        data = self._call(_CREATE_FOLDER, {"parent": parent, "slug": slug})
                        # outro processo pode tê-la criado entretanto: o que conta é existir
                        children = self._list_folders(parent)
                        result = data["assets"]["createFolder"]["responseResult"]
                        if slug not in children:
                            raise WikiError(f"Wiki.js did not create folder {walked!r}: {result}")
                    self._folder_ids[walked] = children[slug]
                parent = self._folder_ids[walked]
            return parent

    def upload_asset(self, folder: str, filename: str, data: bytes, mimetype: str) -> str:
        folder_id = self._folder_id(folder)
        meta = json.dumps({"folderId": folder_id})
        files: list[tuple[str, tuple[str | None, bytes | str, str]]] = [
            ("mediaUpload", (None, meta, "application/json")),
            ("mediaUpload", (filename, data, mimetype)),
        ]
        try:
            response = self._client.post(
                f"{self.url}/u", files=files, headers=self._headers, timeout=UPLOAD_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise WikiUnavailable(f"Wiki.js unreachable: {type(exc).__name__}") from exc
        if response.status_code in (502, 503, 504):
            raise WikiUnavailable(f"Wiki.js answered HTTP {response.status_code}")
        if response.status_code != 200 or response.text.strip() != "ok":
            try:
                reason = str(response.json().get("message"))
            except ValueError:
                reason = response.text[:100].replace("\n", " ")
            raise WikiError(
                f"Wiki.js rejected the upload of {filename!r} "
                f"(HTTP {response.status_code}): {reason}"
            )
        name = "_".join(filename.lower().split())
        return f"/{folder.strip('/')}/{name}"

    def asset_exists(self, path: str) -> bool:
        try:
            response = self._client.head(
                f"{self.url}/{path.lstrip('/')}", headers=self._headers, timeout=10.0
            )
        except httpx.HTTPError as exc:
            raise WikiUnavailable(f"Wiki.js unreachable: {type(exc).__name__}") from exc
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        if response.status_code >= 500:
            raise WikiUnavailable(f"Wiki.js answered HTTP {response.status_code}")
        raise WikiError(f"Wiki.js answered HTTP {response.status_code} to HEAD {path!r}")

    def health(self) -> str:
        return check_health(self.url, "x" if self._headers else "", self._client)
