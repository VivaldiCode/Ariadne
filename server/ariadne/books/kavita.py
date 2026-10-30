"""Cliente da API do Kavita (o leitor web dos livros) e o seu arranque.

O Kavita lê a mesma pasta que o Calibre gere (`BOOKS_DIR`, só de leitura) e mostra os livros a quem
os lê; o Ariadne só precisa de saber **onde** cada livro está no Kavita, para pôr o URL do leitor
nos resultados. A autenticação é a chave de API do utilizador do Ariadne no Kavita (`kavita
bootstrap` cria o utilizador e grava a chave); a chave troca-se por um token de sessão a pedido.

A biblioteca do Kavita tem de ter os metadados ligados (`enableMetadata`): sem isso o Kavita 0.9
não consegue ler os EPUB («Unable to parse any meaningful information»).
"""

import logging
import posixpath
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ariadne.db import models

logger = logging.getLogger(__name__)

BOOK_LIBRARY_TYPE = 2  # Kavita: Manga 0, Comic 1, Book 2, Image 3, LightNovel 4
FILE_GROUPS = [1, 2, 3, 4]  # arquivo, epub, pdf, imagens
HINT = "docker compose up -d kavita (and KAVITA_URL / KAVITA_API_KEY: `ariadne kavita bootstrap`)"
RESCAN_EVERY = 120.0  # segundos entre leituras forçadas da biblioteca quando faltam livros


class KavitaError(Exception):
    """O Kavita recusou ou falhou."""


class KavitaUnavailable(KavitaError):
    """O Kavita não responde, ou a chave não serve."""


@dataclass(frozen=True)
class KavitaSeries:
    id: int
    name: str
    library_id: int


class KavitaClient:
    def __init__(
        self,
        url: str,
        api_key: str = "",
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not url:
            raise KavitaUnavailable(f"KAVITA_URL is not set ({HINT}).")
        self._base = url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._token: str | None = None
        self.last_rescan = float("-inf")  # quando se pediu a última leitura da biblioteca

    # --- ligação ---

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
        retry: bool = True,
    ) -> Any:
        headers = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._session_token()}"
        try:
            response = self._client.request(
                method, self._base + path, json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise KavitaUnavailable(
                f"Kavita is not responding at {self._base} ({type(exc).__name__}). "
                f"Start it with {HINT}."
            ) from exc
        if response.status_code == 401 and authenticated and retry:
            self._token = None  # o token caducou: pede-se outro
            return self._request(
                method, path, json=json, params=params, authenticated=True, retry=False
            )
        if response.status_code in (401, 403):
            raise KavitaUnavailable("Kavita rejected the API key (KAVITA_API_KEY).")
        if response.status_code >= 400:
            raise KavitaError(
                f"Kavita: http {response.status_code} on {path}: {response.text[:200]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def _session_token(self) -> str:
        if self._token is None:
            if not self._api_key:
                raise KavitaUnavailable(f"KAVITA_API_KEY is not set ({HINT}).")
            data = self._request(
                "POST",
                "/api/Plugin/authenticate",
                params={"apiKey": self._api_key, "pluginName": "ariadne"},
                authenticated=False,
            )
            token = data.get("token") if isinstance(data, dict) else None
            if not token:
                raise KavitaUnavailable("Kavita did not give a token (is the API key valid?).")
            self._token = str(token)
        return self._token

    # --- operações ---

    def check(self) -> dict[str, str]:
        """Confirma que o Kavita responde e a chave serve; devolve a versão."""
        info = self._request("GET", "/api/Server/server-info-slim")
        return {"kavita": str(info.get("kavitaVersion", "")) if isinstance(info, dict) else ""}

    def libraries(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/api/Library/libraries") or [])

    def create_library(self, name: str, folders: list[str]) -> int:
        self._request(
            "POST",
            "/api/Library/create",
            json={
                "name": name,
                "type": BOOK_LIBRARY_TYPE,
                "folders": folders,
                "folderWatching": False,
                "includeInDashboard": True,
                "includeInSearch": True,
                "manageCollections": False,
                "manageReadingLists": False,
                "allowScrobbling": False,
                "allowMetadataMatching": False,
                "enableMetadata": True,
                "excludePatterns": [],
                "fileGroupTypes": FILE_GROUPS,
                "metadataProvider": 2,
            },
        )
        return self.find_library(name)

    def find_library(self, name: str) -> int:
        for library in self.libraries():
            if library.get("name") == name:
                return int(library["id"])
        raise KavitaError(f"The library {name!r} does not exist in Kavita.")

    def scan(self, library_id: int, *, force: bool = False) -> None:
        self._request(
            "POST",
            "/api/Library/scan",
            params={"libraryId": library_id, "force": str(force).lower()},
        )

    def series(self, library_id: int) -> list[KavitaSeries]:
        found: list[KavitaSeries] = []
        page = 1
        while True:
            data = self._request(
                "POST",
                "/api/Series/all-v2",
                params={"libraryId": library_id, "PageNumber": page, "PageSize": 100},
                json={
                    "statements": [],
                    "combination": 1,
                    "limitTo": 0,
                    "sortOptions": {"sortField": 1, "isAscending": True},
                },
            )
            rows = data or []
            found += [KavitaSeries(int(r["id"]), str(r["name"]), int(r["libraryId"])) for r in rows]
            if len(rows) < 100:
                return found
            page += 1

    def files(self, series_id: int) -> list[tuple[int, str]]:
        """(id do capítulo, caminho do ficheiro no Kavita) de cada ficheiro da série."""
        out: list[tuple[int, str]] = []
        for volume in (
            self._request("GET", "/api/Series/volumes", params={"seriesId": series_id}) or []
        ):
            for chapter in volume.get("chapters", []):
                for file in chapter.get("files", []):
                    out.append((int(chapter["id"]), str(file.get("filePath", ""))))
        return out

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# --- arranque ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapResult:
    api_key: str
    library_id: int
    created_user: bool
    created_library: bool


def bootstrap(
    url: str,
    username: str,
    password: str,
    email: str,
    *,
    library_name: str = "Ariadne",
    folder: str = "/books",
    timeout: float = 30.0,
) -> BootstrapResult:
    """Prepara o Kavita: cria o utilizador administrador (a 1.ª vez; nas seguintes entra com a
    palavra-passe), a biblioteca de livros sobre `folder` e lança a leitura. Devolve a chave de API
    do utilizador. Idempotente."""
    base = url.rstrip("/")
    with httpx.Client(timeout=timeout) as http:
        try:
            registered = http.post(
                f"{base}/api/Account/register",
                json={"username": username, "password": password, "email": email},
            )
        except httpx.HTTPError as exc:
            raise KavitaUnavailable(
                f"Kavita is not responding at {base} ({type(exc).__name__}). Start it with {HINT}."
            ) from exc
        created_user = registered.status_code == 200
        if created_user:
            user = registered.json()
        else:
            login = http.post(
                f"{base}/api/Account/login", json={"username": username, "password": password}
            )
            if login.status_code != 200:
                raise KavitaError(
                    "Kavita already has users and logging in with KAVITA_ADMIN_USERNAME / "
                    f"KAVITA_ADMIN_PASSWORD failed (http {login.status_code})."
                )
            user = login.json()
    client = KavitaClient(base, str(user["apiKey"]), timeout=timeout)
    try:
        existing = {lib["name"]: int(lib["id"]) for lib in client.libraries()}
        created_library = library_name not in existing
        library_id = (
            client.create_library(library_name, [folder])
            if created_library
            else existing[library_name]
        )
        client.scan(library_id, force=True)
    finally:
        client.close()
    return BootstrapResult(str(user["apiKey"]), library_id, created_user, created_library)


# --- onde está cada livro ------------------------------------------------------------------------


@dataclass
class KavitaSyncReport:
    mapped: int = 0  # livros cujo lugar no Kavita ficou a saber-se agora
    missing: int = 0  # livros que o Kavita ainda não leu
    rescanned: bool = False


def sync_locations(
    session: Session, client: KavitaClient, library_id: int, books_path: str = "/books"
) -> KavitaSyncReport:
    """Liga cada livro (`book.file_path`) à sua série e capítulo no Kavita, pelo caminho do
    ficheiro. Se faltam livros, pede ao Kavita que releia a biblioteca (no máximo de 2 em 2
    minutos); a próxima chamada apanha-os."""
    report = KavitaSyncReport()
    books = session.scalars(
        select(models.Book).where(
            models.Book.status == "active",
            (models.Book.kavita_series_id.is_(None))
            | (models.Book.kavita_library_id != library_id),
        )
    ).all()
    if not books:
        return report
    wanted = {posixpath.join(books_path, book.file_path): book for book in books}
    mapped_series = set(
        session.scalars(
            select(models.Book.kavita_series_id).where(models.Book.kavita_series_id.is_not(None))
        )
    )
    for series in client.series(library_id):
        if series.id in mapped_series:
            continue
        for chapter_id, file_path in client.files(series.id):
            book = wanted.pop(file_path, None)
            if book is None:
                continue
            session.execute(
                update(models.Book)
                .where(models.Book.id == book.id)
                .values(
                    kavita_library_id=library_id,
                    kavita_series_id=series.id,
                    kavita_chapter_id=chapter_id,
                )
            )
            report.mapped += 1
    session.commit()
    report.missing = len(wanted)
    if wanted and time.monotonic() - client.last_rescan > RESCAN_EVERY:
        client.last_rescan = time.monotonic()
        client.scan(library_id)
        report.rescanned = True
        logger.info("Kavita: %d books not yet scanned; requesting a library scan", len(wanted))
    return report
