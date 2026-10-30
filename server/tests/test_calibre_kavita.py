"""Os clientes do Calibre (agente HTTP) e do Kavita, sem os serviços: dobles HTTP.

Os testes contra os serviços do compose estão em `test_books_live.py` (marcas `calibre` e `kavita`).
"""

from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from ariadne.books.backend import LibraryError, LibraryUnavailable
from ariadne.books.calibre import CalibreBackend
from ariadne.books.kavita import KavitaClient, KavitaUnavailable, sync_locations
from ariadne.db import models

BOOK_JSON = {
    "id": 4,
    "title": "Um livro",
    "authors": ["Ana"],
    "language": "por",
    "identifiers": {"isbn": "123"},
    "formats": {"EPUB": "Ana/Um livro (4)/Um livro - Ana.epub"},
}


def calibre(handler: httpx.MockTransport) -> CalibreBackend:
    return CalibreBackend(
        "http://calibre:8080", api_key="k", client=httpx.Client(transport=handler)
    )


def test_calibre_adiciona_com_chave_e_devolve_o_livro(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=BOOK_JSON)

    file = tmp_path / "livro.epub"
    file.write_bytes(b"conteudo")
    book = calibre(httpx.MockTransport(handler)).add(file)

    assert (book.id, book.title, book.authors) == (4, "Um livro", ("Ana",))
    assert book.path_of("epub") == "Ana/Um livro (4)/Um livro - Ana.epub"
    assert seen[0].headers["x-api-key"] == "k" and seen[0].content == b"conteudo"
    assert seen[0].url.params["filename"] == "livro.epub"


def test_calibre_diz_o_que_fazer_quando_nao_responde_ou_recusa(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusado")

    with pytest.raises(LibraryUnavailable, match="docker compose up -d calibre"):
        calibre(httpx.MockTransport(down)).list_books()
    with pytest.raises(LibraryUnavailable, match="CALIBRE_API_KEY"):
        calibre(httpx.MockTransport(lambda r: httpx.Response(401, json={}))).list_books()
    with pytest.raises(LibraryError, match="não existe"):
        calibre(
            httpx.MockTransport(
                lambda r: httpx.Response(404, json={"detail": "livro 9 não existe"})
            )
        ).get(9)
    with pytest.raises(LibraryUnavailable, match="CALIBRE_URL"):
        CalibreBackend("")


def test_calibre_converte_e_remove() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path + "?" + request.url.query.decode()))
        return httpx.Response(200, json=BOOK_JSON if request.method == "POST" else {"removed": 4})

    backend = calibre(httpx.MockTransport(handler))
    backend.convert(4, "EPUB")
    backend.remove(4)

    assert calls == [("POST", "/books/4/convert?to=epub"), ("DELETE", "/books/4?")]


# --- Kavita --------------------------------------------------------------------------------------


def kavita_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/Plugin/authenticate":
            assert request.url.params["apiKey"] == "chave"
            return httpx.Response(200, json={"token": "jwt"})
        assert request.headers["authorization"] == "Bearer jwt"
        if path == "/api/Series/all-v2":
            return httpx.Response(
                200,
                json=[
                    {"id": 6, "name": "Um livro", "libraryId": 3},
                    {"id": 7, "name": "Outro", "libraryId": 3},
                ],
            )
        if path == "/api/Series/volumes":
            series = request.url.params["seriesId"]
            file = {"6": "/books/Ana/Um livro (4)/Um livro - Ana.epub"}.get(series, "/books/x.epub")
            return httpx.Response(
                200, json=[{"chapters": [{"id": int(series) + 100, "files": [{"filePath": file}]}]}]
            )
        if path == "/api/Library/scan":
            return httpx.Response(200)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_kavita_troca_a_chave_por_token_e_localiza_os_livros(db: sessionmaker[Session]) -> None:
    requests: list[httpx.Request] = []
    client = KavitaClient(
        "http://kavita:5000", "chave", client=httpx.Client(transport=kavita_transport(requests))
    )
    with db() as session:
        session.add(
            models.Book(
                id="um-livro", title="Um livro", format="epub", file_size=1, license_class="open",
                file_path="Ana/Um livro (4)/Um livro - Ana.epub", file_sha256="a" * 64,
                license_name="PD",
            )
        )  # fmt: skip
        session.add(
            models.Book(
                id="ainda-nao", title="Ainda não lido", format="epub", file_size=1,
                license_class="open", file_path="Zé/Livro (9)/livro.epub",
                file_sha256="b" * 64, license_name="PD",
            )
        )  # fmt: skip
        session.commit()

        report = sync_locations(session, client, 3, "/books")

        assert (report.mapped, report.missing, report.rescanned) == (1, 1, True)
        book = session.get(models.Book, "um-livro")
        assert book and (book.kavita_library_id, book.kavita_series_id) == (3, 6)
        assert book.kavita_chapter_id == 106
        assert session.get(models.Book, "ainda-nao").kavita_series_id is None  # type: ignore[union-attr]
    paths = [r.url.path for r in requests]
    assert paths.count("/api/Plugin/authenticate") == 1  # o token reutiliza-se
    assert "/api/Library/scan" in paths


def test_kavita_chave_errada_e_servico_em_baixo_dizem_o_que_fazer() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusado")

    with pytest.raises(KavitaUnavailable, match="KAVITA_API_KEY"):
        KavitaClient(
            "http://k", "errada", client=httpx.Client(transport=httpx.MockTransport(refuse))
        ).check()
    with pytest.raises(KavitaUnavailable, match="docker compose up -d kavita"):
        KavitaClient(
            "http://k", "chave", client=httpx.Client(transport=httpx.MockTransport(down))
        ).check()
    with pytest.raises(KavitaUnavailable, match="KAVITA_URL"):
        KavitaClient("")
    with pytest.raises(KavitaUnavailable, match="KAVITA_API_KEY is not set"):
        KavitaClient("http://k", "").check()


def test_nome_de_ficheiro_comprido_mantem_a_extensao() -> None:
    from ariadne.books.calibre import MAX_FILENAME, short_name

    assert short_name("livro.epub") == "livro.epub"
    comprido = "Breves instrucções aos correspondentes " * 8 + ".15668.epub"
    curto = short_name(comprido)
    assert len(curto) == MAX_FILENAME and curto.endswith(".epub")
