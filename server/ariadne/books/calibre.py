"""`CalibreBackend`: fala com o agente HTTP do contentor do Calibre (`deploy/calibre`).

O agente guarda o `calibredb` e o `ebook-convert` e é o único que escreve na biblioteca; o Ariadne
lê os ficheiros do mesmo volume (`BOOKS_DIR`, só de leitura). Toda a chamada leva a chave de API;
sem resposta, a mensagem diz o que fazer.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from ariadne.books.backend import LibraryBackend, LibraryBook, LibraryError, LibraryUnavailable

HINT = "docker compose up -d calibre (and CALIBRE_URL / CALIBRE_API_KEY in the environment)"


def book_from_json(data: dict[str, Any]) -> LibraryBook:
    return LibraryBook(
        id=int(data["id"]),
        title=str(data.get("title") or ""),
        authors=tuple(str(a) for a in data.get("authors") or []),
        language=data.get("language") or None,
        publisher=data.get("publisher") or None,
        published=data.get("published") or None,
        description=str(data.get("description") or ""),
        identifiers={str(k): str(v) for k, v in (data.get("identifiers") or {}).items()},
        tags=tuple(str(t) for t in data.get("tags") or []),
        formats={str(k).upper(): str(v) for k, v in (data.get("formats") or {}).items()},
    )


MAX_FILENAME = 100


def short_name(name: str) -> str:
    """O nome do ficheiro para o Calibre: um título de 200 caracteres perdia a extensão no corte do
    agente («formato não aceite»). Corta-se o nome, nunca a extensão."""
    if len(name) <= MAX_FILENAME:
        return name
    path = Path(name)
    return path.stem[: MAX_FILENAME - len(path.suffix)] + path.suffix


class CalibreBackend(LibraryBackend):
    name = "calibre"

    def __init__(
        self,
        url: str,
        *,
        api_key: str = "",
        timeout: float = 900.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not url:
            raise LibraryUnavailable(f"CALIBRE_URL is not set ({HINT}).")
        self._base = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"X-API-KEY": api_key}
        self._owns_client = client is None

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> Any:
        try:
            response = self._client.request(
                method, self._base + path, params=params, content=content, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise LibraryUnavailable(
                f"Calibre is not responding at {self._base} ({type(exc).__name__}). "
                f"Start it with {HINT}."
            ) from exc
        if response.status_code == 401:
            raise LibraryUnavailable("Calibre rejected the key (wrong CALIBRE_API_KEY).")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", "")
            except ValueError:
                detail = response.text[:200]
            raise LibraryError(f"Calibre: {detail or response.status_code}")
        return response.json()

    def check(self) -> dict[str, str]:
        try:
            response = self._client.get(self._base + "/healthz", timeout=5.0)
        except httpx.HTTPError as exc:
            raise LibraryUnavailable(
                f"Calibre is not responding at {self._base} ({type(exc).__name__}). "
                f"Start it with {HINT}."
            ) from exc
        if response.status_code != 200:
            raise LibraryUnavailable(f"Calibre answered http {response.status_code}.")
        self._call("GET", "/books")  # confirma também a chave
        return {"calibre": str(response.json().get("calibre", ""))}

    def add(
        self, file: Path, *, title: str | None = None, authors: Sequence[str] = ()
    ) -> LibraryBook:
        with file.open("rb") as handle:
            data = handle.read()
        params = {"filename": short_name(file.name)}
        if title:
            params["title"] = title
        if authors:
            params["authors"] = " & ".join(authors)
        return book_from_json(self._call("POST", "/books", params=params, content=data))

    def get(self, book_id: int) -> LibraryBook:
        return book_from_json(self._call("GET", f"/books/{int(book_id)}"))

    def list_books(self) -> list[LibraryBook]:
        return [book_from_json(item) for item in self._call("GET", "/books")]

    def convert(self, book_id: int, to: str) -> LibraryBook:
        return book_from_json(
            self._call("POST", f"/books/{int(book_id)}/convert", params={"to": to.lower()})
        )

    def remove(self, book_id: int) -> None:
        self._call("DELETE", f"/books/{int(book_id)}")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
