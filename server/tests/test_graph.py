"""Fase 5: `neighbors` (MCP e REST) e a curadoria das ligações, sobre dois ZIMs minúsculos."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ariadne import graph
from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.config import Settings
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Link, Source, ToolCall
from ariadne.documents import DocumentInfo, add_aliases, upsert_documents
from ariadne.services import Services
from ariadne.workers.linker import run_linker
from tests.graphzims import REVOLUTION, WIKIBOOKS, WIKIPEDIA, make_graph_dir
from tests.test_level3 import unit_vector
from tests.test_mcp import Live, _serve, with_client

pytestmark = pytest.mark.db

MODEL = "fake/embedder@0000000"
REV = f"{WIKIPEDIA}/{REVOLUTION}"
LISBOA = f"{WIKIPEDIA}/Lisboa"
COZINHA = f"{WIKIPEDIA}/Cozinha"
BOOKS_REV = f"{WIKIBOOKS}/{REVOLUTION}"
ABRIL = f"{WIKIBOOKS}/25_de_Abril"
RECEITAS = f"{WIKIBOOKS}/Receitas"


def settings_for(database_url: str, zims: Path) -> Settings:
    return Settings(
        database_url=database_url,
        zim_dir=zims,
        kiwix_url="http://kiwix.invalido",
        kiwix_public_url="http://kiwix.publico:8801",
    )


def seed(services: Services) -> None:
    """Um grafo pequeno: a Revolução dos Cravos nas duas fontes e vizinhos de vários tipos."""
    with services.sessions() as session:
        docs = [
            (REV, "Revolução dos Cravos", unit_vector(1.0)),
            (BOOKS_REV, "Revolução dos Cravos", None),  # só título
            (ABRIL, "25 de abril", unit_vector(0.90, 2)),  # alias da Wikipédia + proposta vetorial
            (RECEITAS, "Receitas", unit_vector(0.60, 3)),  # só vizinho semântico (0,60)
            (COZINHA, "Cozinha", unit_vector(0.95, 4)),  # vizinho semântico da mesma fonte
            (LISBOA, "Lisboa", None),
        ]
        for document_id, title, vector in docs:
            source_id, path = document_id.split("/", 1)
            source = services.registry.get(source_id)
            assert source is not None
            upsert_documents(session, [DocumentInfo(source, path, title)])
            if vector is not None:
                services.store.put_article_vectors(session, MODEL, {document_id: vector})
        add_aliases(session, {REV: ["25 de Abril"]})
        session.add(
            Link(
                src_document=REV,
                dst_document=LISBOA,
                kind="zim_internal",
                score=1.0,
                status="accepted",
            )
        )
        session.commit()
    run_linker(services)


@pytest.fixture
def live(database_url: str, db: sessionmaker[Session], tmp_path: Path) -> Iterator[Live]:
    for server in _serve(settings_for(database_url, make_graph_dir(tmp_path / "graph")), db):
        seed(server.services)
        yield server


def neighbors_of(live: Live, document_id: str, **arguments: Any) -> dict[str, Any]:
    async def work(client: Client) -> dict[str, Any]:
        result = await client.call_tool("neighbors", {"document_id": document_id, **arguments})
        assert not result.is_error, result.content
        assert result.structured_content is not None
        return dict(result.structured_content)

    return with_client(live, work)


def summary(body: dict[str, Any]) -> list[tuple[str, str, str, float | None]]:
    return [(n["document_id"], n["kind"], n["status"], n["score"]) for n in body["neighbors"]]


# --- ferramenta MCP ---------------------------------------------------------------------------


def test_mcp_neighbors_mostra_a_ligacao_cruzada_entre_zims(live: Live) -> None:
    body = neighbors_of(live, REV)

    assert (body["document_id"], body["title"]) == (REV, "Revolução dos Cravos")
    got = summary(body)
    # 1.º as ligações a outras fontes aceites; depois as propostas, os semânticos e a própria fonte
    assert got[:2] == [
        (BOOKS_REV, "title_match", "accepted", 1.0),  # título igual, mesma língua
        # título alternativo (redirect): só aceite porque o cosseno dos vetores o confirma
        (ABRIL, "title_match", "accepted", pytest.approx(0.90, abs=1e-3)),
    ]
    assert got[2] == (ABRIL, "vector", "proposed", pytest.approx(0.90, abs=1e-3))
    assert [g[:3] for g in got[3:]] == [
        (COZINHA, "semantic", "computed"),
        (RECEITAS, "semantic", "computed"),
        (LISBOA, "zim_internal", "accepted"),
    ]
    first = body["neighbors"][0]
    assert (first["title"], first["source_id"], first["language"]) == (
        "Revolução dos Cravos",
        WIKIBOOKS,
        "pt",
    )
    assert first["link_id"] is not None and first["direction"] == "both"
    assert body["neighbors"][-1]["direction"] == "out"  # REV cita LISBOA
    assert body["neighbors"][2]["model_id"] == MODEL
    assert [n["kind"] for n in body["neighbors"]].count("zim_internal") == 1


def test_mcp_neighbors_vale_nos_dois_sentidos(live: Live) -> None:
    body = neighbors_of(live, BOOKS_REV)
    assert (REV, "title_match", "accepted", 1.0) in summary(body)

    lisboa = neighbors_of(live, LISBOA)
    assert summary(lisboa) == [(REV, "zim_internal", "accepted", 1.0)]
    assert lisboa["neighbors"][0]["direction"] == "in"  # LISBOA é citada por REV


def test_mcp_neighbors_filtra_por_tipo_e_limita(live: Live) -> None:
    only_vector = neighbors_of(live, REV, kinds=["vector"])
    assert [(n["kind"], n["status"]) for n in only_vector["neighbors"]] == [("vector", "proposed")]
    semantic = neighbors_of(live, REV, kinds=["semantic"])  # sem filtro de tipo repetia o Abril
    assert [(n["document_id"], n["status"]) for n in semantic["neighbors"]] == [
        (COZINHA, "computed"),
        (ABRIL, "computed"),
        (RECEITAS, "computed"),
    ]
    assert len(neighbors_of(live, REV, k=2)["neighbors"]) == 2
    assert len(neighbors_of(live, REV, kinds=["zim_internal", "title_match"])["neighbors"]) == 3


def test_mcp_neighbors_nao_mostra_rejeitadas_e_regista_a_chamada(live: Live) -> None:
    with live.sessions() as session:
        link = session.scalars(select(Link).where(Link.kind == "vector")).one()
        link.status = "rejected"
        session.commit()

    body = neighbors_of(live, REV)

    assert "vector" not in {n["kind"] for n in body["neighbors"]}
    # a rejeitada também não volta como vizinho semântico... mas o Abril tem a ligação por título
    [call] = [c for c in live.calls() if c.tool == "neighbors"]
    assert call.arguments == {"document_id": REV, "kinds": None, "k": 20}
    assert call.error is None and call.principal_id == live.principal_id
    assert call.result is not None and call.result["count"] == len(body["neighbors"])
    assert "title_match" in call.result["kinds"]


def test_mcp_neighbors_erros_para_o_agente_ler(live: Live) -> None:
    async def work(client: Client) -> list[tuple[bool, str]]:
        out = []
        for arguments in (
            {"document_id": "zim:nao_existe/X"},
            {"document_id": REV, "kinds": ["inventado"]},
        ):
            result = await client.call_tool("neighbors", arguments)
            out.append((result.is_error, result.content[0].text))  # type: ignore[union-attr]
        return out

    (e1, m1), (e2, m2) = with_client(live, work)

    assert e1 and "Unknown document" in m1
    assert e2 and "inventado" in m2 and "title_match" in m2


# --- REST: vizinhos e curadoria ---------------------------------------------------------------


@pytest.fixture
def api(database_url: str, db: sessionmaker[Session], tmp_path: Path) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, key = create_principal(session, PrincipalKind.user, "curadora", Role.admin)
    settings = settings_for(database_url, make_graph_dir(tmp_path / "graph"))
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {key}"}) as client:
        seed(client.app.state.services)  # type: ignore[attr-defined]
        yield client


def test_rest_pede_chave(api: TestClient) -> None:
    api.headers.pop("Authorization")
    for url in (f"/api/neighbors?document_id={REV}", "/api/links"):
        assert api.get(url).status_code == 401
    assert api.post("/api/links", json={}).status_code == 401


def test_rest_neighbors_recebe_o_id_em_parametro(api: TestClient) -> None:
    resp = api.get("/api/neighbors", params={"document_id": REV, "kinds": "title_match,vector"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == REV
    assert [(n["document_id"], n["kind"], n["status"]) for n in body["neighbors"]] == [
        (BOOKS_REV, "title_match", "accepted"),
        (ABRIL, "title_match", "accepted"),
        (ABRIL, "vector", "proposed"),
    ]
    assert api.get("/api/neighbors", params={"document_id": "zim:nao_existe/X"}).status_code == 404
    assert api.get("/api/neighbors", params={"document_id": REV, "kinds": "x"}).status_code == 400
    assert api.get("/api/neighbors").status_code == 422
    assert api.get(f"/api/documents/{REV}").status_code == 200  # a rota do documento segue a servir


def test_rest_neighbors_de_um_id_que_acaba_em_links(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    """Com o id no caminho, `…/links` seria ambíguo; em parâmetro não há dúvida."""
    tricky = f"{WIKIPEDIA}/pasta/links"
    services: Services = api.app.state.services  # type: ignore[attr-defined]
    source = services.registry.get(WIKIPEDIA)
    assert source is not None
    with db() as session:
        upsert_documents(session, [DocumentInfo(source, "pasta/links", "Links")])
        session.commit()

    resp = api.get("/api/neighbors", params={"document_id": tricky})

    assert resp.status_code == 200 and resp.json()["document_id"] == tricky


def test_rest_lista_as_propostas_para_curadoria(api: TestClient) -> None:
    resp = api.get("/api/links", params={"status": "proposed"})

    assert resp.status_code == 200
    page = resp.json()
    assert page["next_cursor"] is None
    [proposal] = page["items"]
    assert (proposal["kind"], proposal["status"], proposal["created_by"]) == (
        "vector",
        "proposed",
        "linker",
    )
    assert proposal["model_id"] == MODEL
    assert {proposal["src_document"], proposal["dst_document"]} == {REV, ABRIL}
    assert {proposal["src_title"], proposal["dst_title"]} == {"Revolução dos Cravos", "25 de abril"}
    assert (proposal["reviewed_by"], proposal["reviewed_at"]) == (None, None)
    everything = api.get("/api/links").json()["items"]
    assert {link["kind"] for link in everything} == {"title_match", "vector", "zim_internal"}
    kind = api.get("/api/links", params={"kind": "zim_internal"}).json()["items"]
    assert [link["kind"] for link in kind] == ["zim_internal"]
    of_lisboa = api.get("/api/links", params={"document_id": LISBOA}).json()["items"]
    assert [link["kind"] for link in of_lisboa] == ["zim_internal"]
    assert len(api.get("/api/links", params={"limit": 1}).json()["items"]) == 1
    assert api.get("/api/links", params={"status": "talvez"}).status_code == 422
    assert api.get("/api/links", params={"limit": 0}).status_code == 422


def test_rest_aceitar_e_rejeitar_uma_proposta(api: TestClient) -> None:
    [proposal] = api.get("/api/links", params={"status": "proposed"}).json()["items"]

    accepted = api.post(f"/api/links/{proposal['id']}", json={"status": "accepted"})

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["reviewed_by"] == str(api.get("/api/whoami").json()["id"])
    assert accepted.json()["reviewed_at"] is not None
    assert api.get("/api/links", params={"status": "proposed"}).json()["items"] == []
    kinds = api.get("/api/neighbors", params={"document_id": REV, "kinds": "vector"}).json()
    assert [(n["status"], n["document_id"]) for n in kinds["neighbors"]] == [("accepted", ABRIL)]

    rejected = api.post(f"/api/links/{proposal['id']}", json={"status": "rejected"})

    assert rejected.json()["status"] == "rejected"
    assert (
        api.get("/api/neighbors", params={"document_id": REV, "kinds": "vector"}).json()[
            "neighbors"
        ]
        == []
    )
    # nem como vizinho semântico (o ABRIL é o mais parecido do REV, mas a curadoria disse que não)
    semantic = api.get("/api/neighbors", params={"document_id": REV, "kinds": "semantic"}).json()
    assert ABRIL not in [n["document_id"] for n in semantic["neighbors"]]
    # o linker não ressuscita uma rejeitada
    services: Services = api.app.state.services  # type: ignore[attr-defined]
    with services.sessions() as session:
        session.execute(update(DocumentRow).values(linked_at=None))
        session.commit()
    run_linker(services)
    rejected_ids = [
        x["id"] for x in api.get("/api/links", params={"status": "rejected"}).json()["items"]
    ]
    assert rejected_ids == [proposal["id"]]
    assert api.get("/api/links", params={"status": "proposed"}).json()["items"] == []


def test_rest_revisao_invalida_ou_de_ligacao_inexistente(api: TestClient) -> None:
    assert api.post("/api/links/999999", json={"status": "accepted"}).status_code == 404
    assert api.post("/api/links/1", json={"status": "apagada"}).status_code == 422
    assert api.post("/api/links/1", json={}).status_code == 422


def test_rest_ligacao_manual_vale_nos_dois_sentidos_e_e_idempotente(api: TestClient) -> None:
    created = api.post("/api/links", json={"src_document": RECEITAS, "dst_document": REV})

    assert created.status_code == 201
    link = created.json()
    assert (link["kind"], link["status"], link["created_by"], link["score"]) == (
        "manual",
        "accepted",
        "curadora",
        1.0,
    )
    assert (link["src_document"], link["dst_document"]) == (RECEITAS, REV)  # guardada canónica
    for document_id, other in ((REV, RECEITAS), (RECEITAS, REV)):
        neighbors = api.get(
            "/api/neighbors", params={"document_id": document_id, "kinds": "manual"}
        )
        assert [n["document_id"] for n in neighbors.json()["neighbors"]] == [other]

    again = api.post(
        "/api/links", json={"src_document": REV, "dst_document": RECEITAS}
    )  # invertida

    assert again.status_code == 200 and again.json()["id"] == link["id"]
    api.post(f"/api/links/{link['id']}", json={"status": "rejected"})
    reaccepted = api.post("/api/links", json={"src_document": REV, "dst_document": RECEITAS})
    assert (reaccepted.status_code, reaccepted.json()["status"]) == (200, "accepted")


def test_rest_ligacao_manual_valida_os_documentos(api: TestClient) -> None:
    same = api.post("/api/links", json={"src_document": REV, "dst_document": REV})
    unknown = api.post("/api/links", json={"src_document": REV, "dst_document": "zim:x/Y"})

    assert same.status_code == 400 and "different documents" in same.json()["detail"]
    assert unknown.status_code == 404 and "zim:x/Y" in unknown.json()["detail"]
    assert api.post("/api/links", json={"src_document": REV}).status_code == 422


def test_rest_regista_as_chamadas(api: TestClient) -> None:
    api.get("/api/neighbors", params={"document_id": REV})
    api.get("/api/links", params={"status": "proposed"})
    api.post("/api/links", json={"src_document": RECEITAS, "dst_document": REV})
    sessions: sessionmaker[Session] = api.app.state.services.sessions  # type: ignore[attr-defined]
    with sessions() as session:
        calls = list(session.scalars(select(ToolCall).order_by(ToolCall.id)))

    assert [c.tool for c in calls] == ["neighbors", "links", "link_create"]
    assert calls[2].result is not None and calls[2].result["created"] is True


def test_rest_so_pessoas_criam_ou_revem_ligacoes(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    with db() as session, session.begin():
        _, agent_key = create_principal(session, PrincipalKind.agent, "robo")
    [proposal] = api.get("/api/links", params={"status": "proposed"}).json()["items"]
    api.headers["Authorization"] = f"Bearer {agent_key}"

    create = api.post("/api/links", json={"src_document": RECEITAS, "dst_document": REV})
    review = api.post(f"/api/links/{proposal['id']}", json={"status": "accepted"})

    assert (create.status_code, review.status_code) == (403, 403)
    assert "user" in create.json()["detail"]
    # ler continua a ser permitido
    assert api.get("/api/links", params={"status": "proposed"}).json()["items"][0]["status"] == (
        "proposed"
    )
    assert api.get("/api/neighbors", params={"document_id": REV}).status_code == 200


def test_rest_curadoria_pagina_por_cursor_sem_repetir_nem_perder(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    services: Services = api.app.state.services  # type: ignore[attr-defined]
    with services.sessions() as session:
        # mais propostas, com scores repetidos e nulos
        rows = [
            (f"{WIKIBOOKS}/P{n}", f"{WIKIPEDIA}/P{n}", None if n % 4 == 0 else 0.90 + (n % 3) / 100)
            for n in range(10)
        ]
        for src, dst, _ in rows:
            for document_id in (src, dst):
                source_id, path = document_id.split("/", 1)
                source = services.registry.get(source_id)
                assert source is not None
                upsert_documents(session, [DocumentInfo(source, path, path)])
        session.add_all(
            Link(src_document=src, dst_document=dst, kind="vector", score=score, status="proposed")
            for src, dst, score in rows
        )
        session.commit()
    everything = [x["id"] for x in api.get("/api/links", params={"limit": 200}).json()["items"]]

    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": 4}
        if cursor:
            params["cursor"] = cursor
        page = api.get("/api/links", params=params).json()
        seen += [x["id"] for x in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == everything and len(set(seen)) == len(seen)
    assert pages == -(-len(everything) // 4)
    assert api.get("/api/links", params={"cursor": "lixo"}).status_code == 400


# --- neighbors: limite e ordem na base de dados -----------------------------------------------


def test_neighbors_limita_os_links_da_propria_fonte_ao_que_cabe_em_k(api: TestClient) -> None:
    services: Services = api.app.state.services  # type: ignore[attr-defined]
    source = services.registry.get(WIKIPEDIA)
    assert source is not None
    with services.sessions() as session:
        upsert_documents(session, [DocumentInfo(source, f"Z{n:02d}", f"Z{n}") for n in range(30)])
        session.add_all(
            Link(src_document=REV, dst_document=f"{WIKIPEDIA}/Z{n:02d}", kind="zim_internal")
            for n in range(30)
        )
        session.commit()

        small = graph.neighbors(services, session, REV, k=4)
        big = graph.neighbors(services, session, REV, k=100)

    # as ligações a outras fontes primeiro; os `zim_internal` só preenchem o que sobra
    assert [n.kind for n in small.neighbors] == ["title_match", "title_match", "vector", "semantic"]
    assert len(big.neighbors) == 3 + 2 + 31  # 3 cruzadas, 2 semânticos, 31 internos (30 + Lisboa)
    assert [n.kind for n in big.neighbors][:5].count("zim_internal") == 0


def test_neighbors_ignora_fontes_que_sumiram(api: TestClient) -> None:
    services: Services = api.app.state.services  # type: ignore[attr-defined]
    with services.sessions() as session:
        session.execute(update(Source).where(Source.id == WIKIBOOKS).values(status="missing"))
        session.commit()

        result = graph.neighbors(services, session, REV)

    assert {n.source_id for n in result.neighbors} == {WIKIPEDIA}  # Cozinha (semântico) e Lisboa
