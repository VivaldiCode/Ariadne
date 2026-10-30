from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.config import Settings
from ariadne.db.models import ToolCall
from tests.fakes import PT_BOOK, default_fake

pytestmark = pytest.mark.db

PT = "zim:wikipedia_pt_teste"


@pytest.fixture
def api(database_url: str, db: sessionmaker[Session], zim_dir: Path) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, key = create_principal(session, PrincipalKind.user, "pessoa", Role.admin)
    settings = Settings(
        database_url=database_url,
        zim_dir=zim_dir,
        kiwix_url="http://kiwix.invalido",
        kiwix_public_url="http://kiwix.publico:8801",
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {key}"}) as client:
        client.app.state.services.kiwix = default_fake().client()  # type: ignore[attr-defined]
        yield client


def tool_calls(api: TestClient) -> list[ToolCall]:
    sessions: sessionmaker[Session] = api.app.state.services.sessions  # type: ignore[attr-defined]
    with sessions() as session:
        return list(session.scalars(select(ToolCall).order_by(ToolCall.id)))


def test_endpoints_pedem_chave(api: TestClient) -> None:
    api.headers.pop("Authorization")
    for url in ("/api/search?q=x", f"/api/documents/{PT}/Lisboa", "/api/sources"):
        assert api.get(url).status_code == 401


def test_search(api: TestClient) -> None:
    resp = api.get("/api/search", params={"q": "capital", "lang": "pt", "k": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert [h["document_id"] for h in body["results"]] == [f"{PT}/Lisboa", f"{PT}/Portugal"]
    assert body["results"][0]["kiwix_url"] == f"http://kiwix.publico:8801/content/{PT_BOOK}/Lisboa"
    assert body["results"][0]["level"] == 1 and body["results"][0]["wiki_url"] is None
    assert isinstance(body["query_id"], int)
    [call] = tool_calls(api)
    assert (call.tool, call.query_id, call.session_id, call.error) == (
        "search",
        body["query_id"],
        None,
        None,
    )
    assert call.arguments == {"query": "capital", "lang": "pt", "sources": None, "k": 2}


def test_search_com_fontes_repetidas_ou_separadas_por_virgulas(api: TestClient) -> None:
    for query in (
        "sources=wikipedia_pt_teste",
        "sources=a,wikipedia_pt_teste",
        "sources=a&sources=zim:wikipedia_pt_teste",
    ):
        assert api.get(f"/api/search?q=x&{query}").status_code == 200


def test_search_erros(api: TestClient) -> None:
    assert api.get("/api/search").status_code == 422  # falta q
    resp = api.get("/api/search", params={"q": "x", "lang": "en"})
    assert resp.status_code == 400 and "Available languages: pt" in resp.json()["detail"]
    broken = default_fake()
    broken.down.add(PT_BOOK)
    api.app.state.services.kiwix = broken.client()  # type: ignore[attr-defined]
    assert api.get("/api/search", params={"q": "x"}).status_code == 503
    assert [c.tool for c in tool_calls(api)] == ["search", "search"]
    assert all(c.error for c in tool_calls(api))


def test_documento(api: TestClient) -> None:
    resp = api.get(f"/api/documents/{PT}/Lisboa", params={"section": "terramoto"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == f"{PT}/Lisboa" and body["section"] == "Terramoto"
    assert body["text"].startswith("### Terramoto")
    assert body["links"] == []
    [call] = tool_calls(api)
    assert call.tool == "read" and call.result is not None and "text" not in call.result


def test_documento_erros(api: TestClient) -> None:
    assert api.get(f"/api/documents/{PT}/Nao_existe").status_code == 404
    assert api.get("/api/documents/zim:outra/Lisboa").status_code == 404
    assert api.get(f"/api/documents/{PT}/Lisboa", params={"section": "Economia"}).status_code == 404
    assert api.get(f"/api/documents/{PT}/_res_/style.css").status_code == 404
    assert api.get("/api/documents/sem-barra").status_code == 404
    redirect = api.get(f"/api/documents/{PT}/Capital_de_Portugal")
    assert redirect.json()["redirected_from"] == f"{PT}/Capital_de_Portugal"


def test_sources(api: TestClient) -> None:
    resp = api.get("/api/sources")

    assert resp.status_code == 200
    [source] = resp.json()
    assert source.pop("size_bytes") > 0  # o ficheiro ZIM (espaço ocupado)
    assert source == {
        "id": PT,
        "title": "Wikipédia (teste)",
        "kind": "zim",
        "language": "pt",
        "version": "2026-09",
        "articles": 4,
        "wing": "wikipedia_pt_teste",
        "rooms": {"count": 0, "top": []},  # sem vetores de artigo ainda não há salas
        "content_class": "article",
        "content_type": "article",
        "archive_only": False,
        "license_class": "open",
    }
    assert [c.tool for c in tool_calls(api)] == ["sources"]


def test_nul_e_surrogates_nao_dao_500_nem_perdem_o_registo(api: TestClient) -> None:
    resp = api.get("/api/search", params={"q": "capital\x00"})
    assert resp.status_code == 200
    assert api.get(f"/api/documents/{PT}/Lisboa%00", params={"section": "x\x00"}).status_code == 404
    calls = tool_calls(api)
    assert [c.tool for c in calls] == ["search", "read"]
    assert calls[0].arguments["query"] == "capital"
    assert "\x00" not in str(calls[1].arguments) and calls[1].error is not None
