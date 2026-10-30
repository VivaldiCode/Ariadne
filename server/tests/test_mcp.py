"""MCP de ponta a ponta: a app sobe num uvicorn e fala-se com ela pelo cliente MCP do SDK."""

import socket
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, Scope, create_principal
from ariadne.config import Settings
from ariadne.db.models import Document, QueryLog, QueryResult, Room, ToolCall
from ariadne.licenses import mark_source_private
from ariadne.mcp.server import RateLimiter
from ariadne.services import Services
from tests.fakes import default_fake

pytestmark = pytest.mark.db

PT = "zim:wikipedia_pt_teste"


@dataclass
class Live:
    url: str  # http://localhost:<porta>
    key: str
    principal_id: int
    sessions: sessionmaker[Session]
    services: Services

    @property
    def mcp(self) -> str:
        return f"{self.url}/mcp"

    def new_principal(self, name: str, scope: Scope = Scope.full) -> tuple[int, str]:
        with self.sessions() as session, session.begin():
            principal, key = create_principal(
                session, PrincipalKind.agent, name, Role.member, scope
            )
            return principal.id, key

    def calls(self) -> list[ToolCall]:
        with self.sessions() as session:
            return list(session.scalars(select(ToolCall).order_by(ToolCall.id)))


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("localhost", 0))
        return int(sock.getsockname()[1])


def _serve(settings: Settings, sessions: sessionmaker[Session]) -> Iterator[Live]:
    with sessions() as session, session.begin():
        principal, key = create_principal(session, PrincipalKind.agent, "agente-mcp")
        principal_id = principal.id
    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="localhost", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "o servidor não arrancou"
        time.sleep(0.02)
    try:
        yield Live(f"http://localhost:{port}", key, principal_id, sessions, app.state.services)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture
def live(database_url: str, db: sessionmaker[Session], zim_dir: Path) -> Iterator[Live]:
    settings = Settings(
        database_url=database_url,
        zim_dir=zim_dir,
        kiwix_url="http://kiwix.invalido",
        kiwix_public_url="http://kiwix.publico:8801",
    )
    for live_server in _serve(settings, db):
        # O kiwix de brincar substitui o cliente HTTP; a app lê-o de app.state a cada chamada.
        live_server.services.kiwix = default_fake().client()
        yield live_server


def with_client[T](
    live: Live,
    work: Callable[[Client], Coroutine[Any, Any, T]],
    mode: str = "legacy",
    key: str | None = None,
) -> T:
    async def main() -> T:
        headers = {"Authorization": f"Bearer {key or live.key}"}
        async with httpx2.AsyncClient(headers=headers) as http:
            transport = streamable_http_client(live.mcp, http_client=http)
            async with Client(transport, mode=mode) as client:
                return await work(client)

    return anyio.run(main)


# --- autenticação -------------------------------------------------------------------------------


def test_sem_chave_valida_401(live: Live) -> None:
    for headers in ({}, {"Authorization": "Bearer ariadne_errada"}, {"Authorization": "Basic abc"}):
        for method in ("POST", "GET", "DELETE"):
            resp = httpx2.request(
                method, live.mcp, headers=headers, json={} if method == "POST" else None
            )
            assert resp.status_code == 401, (method, headers)
            assert resp.headers["www-authenticate"] == "Bearer"
            assert "api key" in resp.json()["detail"].lower()
    assert httpx2.get(f"{live.url}/health").status_code == 200  # o resto não pede chave
    assert live.calls() == []


def test_cliente_sem_chave_falha(live: Live) -> None:
    async def work(client: Client) -> None:
        await client.list_tools()

    with pytest.raises(Exception):  # noqa: B017 - o SDK embrulha o 401 num ExceptionGroup
        with_client(live, work, key="ariadne_errada")


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "teste", "version": "1"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_sessao_so_continua_com_a_chave_que_a_abriu(live: Live) -> None:
    _, other_key = live.new_principal("outro-agente")
    opened = httpx2.post(
        live.mcp, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {live.key}"}
    )
    assert opened.status_code == 200
    session_id = opened.headers["mcp-session-id"]
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}

    mine = httpx2.post(
        live.mcp,
        json=ping,
        headers={
            **MCP_HEADERS,
            "Authorization": f"Bearer {live.key}",
            "Mcp-Session-Id": session_id,
        },
    )
    theirs = httpx2.post(
        live.mcp,
        json=ping,
        headers={
            **MCP_HEADERS,
            "Authorization": f"Bearer {other_key}",
            "Mcp-Session-Id": session_id,
        },
    )

    assert mine.status_code == 200
    assert theirs.status_code == 404  # responde como se a sessão não existisse


# --- ferramentas --------------------------------------------------------------------------------


def test_list_tools(live: Live) -> None:
    async def work(client: Client) -> dict[str, Any]:
        return {tool.name: tool for tool in (await client.list_tools()).tools}

    tools = with_client(live, work)

    assert set(tools) == {
        "search", "read", "neighbors", "translations", "translate", "sources", "rooms", "feedback",
    }  # fmt: skip
    for tool in tools.values():
        assert tool.description and tool.description.isascii()  # descrições em inglês
    assert tools["search"].input_schema["required"] == ["query"]
    assert set(tools["search"].input_schema["properties"]) == {
        "query", "lang", "sources", "k", "wings", "rooms",
    }  # fmt: skip
    assert tools["read"].input_schema["required"] == ["document_id"]
    assert set(tools["read"].input_schema["properties"]) == {
        "document_id",
        "section",
        "max_chars",
        "offset",
    }
    assert tools["neighbors"].input_schema["required"] == ["document_id"]
    assert set(tools["neighbors"].input_schema["properties"]) == {"document_id", "kinds", "k"}
    assert tools["translations"].input_schema["required"] == ["document_id"]
    assert set(tools["translations"].input_schema["properties"]) == {"document_id"}
    assert set(tools["feedback"].input_schema["required"]) == {"query_id", "document_id", "useful"}
    assert "sources" not in tools["sources"].input_schema.get("required", [])
    assert [
        tools[n].annotations.read_only_hint  # type: ignore[union-attr]
        for n in ("search", "read", "neighbors", "sources")
    ] == [True] * 4
    assert tools["feedback"].annotations.read_only_hint is False  # type: ignore[union-attr]


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_pesquisar_ler_e_dar_feedback(live: Live, db: sessionmaker[Session], mode: str) -> None:
    async def work(client: Client) -> dict[str, Any]:
        sources = await client.call_tool("sources", {})
        found = await client.call_tool("search", {"query": "capital", "k": 2})
        assert found.structured_content is not None
        top = found.structured_content["results"][0]["document_id"]
        doc = await client.call_tool("read", {"document_id": top, "section": "História"})
        fb = await client.call_tool(
            "feedback",
            {"query_id": found.structured_content["query_id"], "document_id": top, "useful": True},
        )
        assert not any(r.is_error for r in (sources, found, doc, fb))
        return {
            "sources": sources.structured_content,
            "search": found.structured_content,
            "read": doc.structured_content,
            "feedback": fb.structured_content,
        }

    out = with_client(live, work, mode=mode)

    assert out["sources"]["sources"][0]["id"] == PT
    hits = out["search"]["results"]
    assert [h["document_id"] for h in hits] == [f"{PT}/Lisboa", f"{PT}/Portugal"]
    assert (
        hits[0]["kiwix_url"]
        == "http://kiwix.publico:8801/content/wikipedia_pt_teste_2026-09/Lisboa"
    )
    assert out["read"]["section"] == "História"
    assert "terramoto de 1755" in out["read"]["text"]
    assert out["feedback"] == {"ok": True, "updated": 1}

    calls = live.calls()
    assert [c.tool for c in calls] == ["sources", "search", "read", "feedback"]
    assert {c.principal_id for c in calls} == {live.principal_id}
    assert all(c.error is None and c.latency_ms is not None for c in calls)
    search_call, read_call = calls[1], calls[2]
    assert search_call.arguments == {"query": "capital", "lang": None, "sources": None, "k": 2}
    assert search_call.result == {
        "query_id": out["search"]["query_id"],
        "count": 2,
        "document_ids": [f"{PT}/Lisboa", f"{PT}/Portugal"],
    }
    assert search_call.query_id == calls[3].query_id == out["search"]["query_id"]
    assert read_call.result is not None
    assert "text" not in read_call.result and read_call.result["document_id"] == f"{PT}/Lisboa"
    # Só o Mcp-Session-Id (protocolo com handshake) identifica a sessão; o modo moderno não tem.
    session_ids = {c.session_id for c in calls}
    with db() as session:
        logged = session.get(QueryLog, out["search"]["query_id"])
        assert logged is not None
        assert logged.session_id == search_call.session_id
        results = {r.document_id: r for r in session.scalars(select(QueryResult))}
    if mode == "legacy":
        assert len(session_ids) == 1 and None not in session_ids
    else:
        assert session_ids == {None}
    assert results[f"{PT}/Lisboa"].used is True and results[f"{PT}/Lisboa"].feedback == 1
    assert results[f"{PT}/Portugal"].used is None


def test_feedback_de_cliente_nao_mexe_no_ranking_do_dono(
    live: Live, db: sessionmaker[Session]
) -> None:
    """O feedback de uma chave `mcp` fica registado (métricas), mas não aquece o documento; o de
    uma chave `full` aquece (+2)."""
    _, client_key = live.new_principal("cliente-externo", Scope.mcp)
    portugal = f"{PT}/Portugal"

    def heat() -> float:
        with db() as session:
            row = session.get(Document, portugal)
            assert row is not None
            return row.heat

    def useful(key: str) -> int:
        async def work(client: Client) -> int:
            found = await client.call_tool("search", {"query": "capital", "k": 2})
            assert found.structured_content is not None
            query_id = int(found.structured_content["query_id"])
            fb = await client.call_tool(
                "feedback", {"query_id": query_id, "document_id": portugal, "useful": True}
            )
            assert not fb.is_error
            return query_id

        return with_client(live, work, key=key)

    query_id = useful(client_key)
    before = heat()
    with db() as session:
        feedback = session.scalar(
            select(QueryResult.feedback).where(
                QueryResult.query_id == query_id, QueryResult.document_id == portugal
            )
        )
    assert feedback == 1  # registado
    useful(client_key)  # outra pesquisa e outro feedback do cliente: o calor não sobe por isso
    assert heat() < before + 1
    before = heat()
    useful(live.key)
    assert heat() >= before + 2  # o do dono aquece (mais os 0,3 de ser mostrado)


def test_chave_mcp_so_ve_conteudo_aberto(live: Live, db: sessionmaker[Session]) -> None:
    """Uma chave de cliente externo (âmbito `mcp`) não vê nada privado: nem nos resultados, nem a
    ler, nem nos vizinhos, nem na lista de fontes; e o erro é o de um documento que não existe."""
    _, client_key = live.new_principal("cliente-externo", Scope.mcp)
    lisboa = f"{PT}/Lisboa"

    async def calls(client: Client) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, args in (
            ("sources", {}),
            ("search", {"query": "capital", "k": 2}),
            ("search_pt", {"query": "capital", "k": 2, "sources": [PT]}),
            ("read", {"document_id": lisboa}),
            ("neighbors", {"document_id": lisboa}),
            ("translations", {"document_id": lisboa}),
            ("neighbors_unread", {"document_id": f"{PT}/Outro"}),  # ainda sem linha na base
        ):
            got = await client.call_tool(name.split("_")[0], args)
            out[name] = got
        return out

    before = with_client(live, calls, key=client_key)  # tudo aberto: vê tudo
    assert not any(r.is_error for name, r in before.items() if name != "neighbors_unread")
    assert "not found" not in str(before["neighbors_unread"].content)
    assert [s["id"] for s in before["sources"].structured_content["sources"]] == [PT]
    assert before["search"].structured_content["results"][0]["document_id"] == lisboa

    with db() as session, session.begin():
        mark_source_private(session, PT)
        room = Room(wing="wikipedia_pt_teste", label="Lisboa", model_id="m@1", centroid=[1.0] * 384)
        session.add(room)
        session.flush()
        room_id = room.id
    live.services.sync_sources()

    async def leaks(client: Client) -> list[Any]:
        return [
            await client.call_tool("read", {"document_id": "zim:nao_existe/X"}),
            await client.call_tool(
                "search", {"query": "capital", "rooms": [room_id], "wings": ["outra"]}
            ),
            await client.call_tool("rooms", {}),
        ]

    unknown, by_room, rooms_list = with_client(live, leaks, key=client_key)
    assert unknown.is_error and by_room.is_error
    for result in (unknown, by_room):  # as mensagens não dizem a fonte nem a ala privadas
        assert "wikipedia_pt_teste" not in str(result.content)
    assert f"Unknown rooms: [{room_id}]" in str(by_room.content)
    assert rooms_list.structured_content["rooms"] == []

    after = with_client(live, calls, key=client_key)
    assert after["sources"].structured_content == {"sources": []}
    for name in ("search", "search_pt"):
        result = after[name]
        assert result.is_error or result.structured_content["results"] == []
    assert PT not in str(after["search"].content)
    # Pedida pelo id, o erro só repete o que o cliente pediu: a fonte privada não é «disponível».
    assert "Available: (none)" in str(after["search_pt"].content)
    for name in ("read", "neighbors", "translations"):
        assert after[name].is_error, name
        assert f"Document not found: {lisboa!r}" in str(after[name].content), name
    assert after["neighbors_unread"].is_error
    assert f"Document not found: '{PT}/Outro'" in str(after["neighbors_unread"].content)

    full = with_client(live, calls)  # a chave de sempre continua a ver o privado
    assert not full["read"].is_error and not full["search"].is_error
    assert full["search"].structured_content["results"][0]["document_id"] == lisboa


def test_limite_de_pedidos_por_chave() -> None:
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert limiter.retry_after(1) is None and limiter.retry_after(1) is None
    assert limiter.retry_after(1) == 60
    assert limiter.retry_after(2) is None  # cada chave tem a sua janela
    now[0] = 59.5
    assert limiter.retry_after(1) == 1
    now[0] = 60.0
    assert limiter.retry_after(1) is None


def test_chave_mcp_tem_limite_de_pedidos(
    database_url: str, db: sessionmaker[Session], zim_dir: Path
) -> None:
    settings = Settings(
        database_url=database_url,
        zim_dir=zim_dir,
        kiwix_url="http://kiwix.invalido",
        mcp_client_rate_per_minute=3,
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    for live in _serve(settings, db):
        _, client_key = live.new_principal("cliente-externo", Scope.mcp)
        url = live.mcp

        def post(key: str, url: str = url) -> httpx2.Response:
            headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
            return httpx2.post(url, headers=headers, json=body)

        codes = [post(client_key).status_code for _ in range(4)]
        assert codes[:3] != [429] * 3 and codes[3] == 429
        limited = post(client_key)
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
        assert {post(live.key).status_code for _ in range(5)} != {429}  # as chaves `full` não


def test_erros_chegam_ao_agente_e_ficam_registados(live: Live) -> None:
    async def work(client: Client) -> list[tuple[bool, str]]:
        calls = [
            ("read", {"document_id": f"{PT}/Nao_existe"}),
            ("read", {"document_id": f"{PT}/Lisboa", "section": "Economia"}),
            ("search", {"query": "x", "lang": "xx"}),
            ("search", {"query": "x", "sources": ["nao-existe"]}),
            ("feedback", {"query_id": 424242, "document_id": f"{PT}/Lisboa", "useful": True}),
            ("search", {"k": 3}),  # falta o argumento obrigatório
        ]
        out = []
        for name, arguments in calls:
            result = await client.call_tool(name, arguments)
            out.append((result.is_error, result.content[0].text))  # type: ignore[union-attr]
        return out

    out = with_client(live, work)

    assert all(is_error for is_error, _ in out)
    messages = [text for _, text in out]
    assert "Document not found" in messages[0]
    assert "Available sections: 'Lisboa', 'História'" in messages[1]
    assert "Available languages: pt" in messages[2]
    assert "Available: zim:wikipedia_pt_teste" in messages[3]
    assert "Search 424242 not found" in messages[4]
    calls = live.calls()
    assert len(calls) == 5  # o último nem chegou à ferramenta (argumentos inválidos)
    assert calls[0].error is not None and "DocumentNotFound" in calls[0].error
    assert all(c.error for c in calls)
    assert calls[0].result is None


def test_nul_e_surrogates_sao_limpos_e_o_registo_fica(live: Live) -> None:
    async def work(client: Client) -> list[tuple[bool, str]]:
        out = []
        for name, arguments in (
            ("search", {"query": "capital\x00", "k": 1}),
            ("read", {"document_id": f"{PT}/Lisboa\x00", "section": "Terra\x00moto"}),
            ("read", {"document_id": f"{PT}/Lisboa", "section": "Eco\x00nomia"}),
        ):
            result = await client.call_tool(name, arguments)
            out.append((result.is_error, result.content[0].text))  # type: ignore[union-attr]
        return out

    out = with_client(live, work)

    assert [is_error for is_error, _ in out] == [False, False, True]
    assert "Economia" in out[2][1] and "SELECT" not in out[2][1] and "psycopg" not in out[2][1]
    calls = live.calls()
    assert [c.tool for c in calls] == ["search", "read", "read"]
    assert calls[0].arguments["query"] == "capital" and "\x00" not in str(calls[1].arguments)


def test_kiwix_em_baixo_e_erro_da_ferramenta(live: Live) -> None:
    default = default_fake()
    default.down.add("wikipedia_pt_teste_2026-09")
    live.services.kiwix = default.client()

    async def work(client: Client) -> tuple[bool, str]:
        result = await client.call_tool("search", {"query": "capital"})
        return result.is_error, result.content[0].text  # type: ignore[union-attr]

    is_error, text = with_client(live, work)

    assert is_error and "Search unavailable" in text
    assert "SearchUnavailable" in (live.calls()[0].error or "")


# --- contra o kiwix-serve e o ZIM reais do compose ---------------------------------------------


@pytest.mark.kiwix
def test_claude_pesquisa_e_cita_a_biblioteca_real(
    database_url: str, db: sessionmaker[Session], kiwix_url: str, real_zim: Path
) -> None:
    settings = Settings(
        database_url=database_url,
        zim_dir=real_zim.parent,
        kiwix_url=kiwix_url,
        kiwix_public_url=kiwix_url,
    )

    async def work(client: Client) -> dict[str, Any]:
        found = await client.call_tool("search", {"query": "ray charles blindness", "k": 3})
        assert found.structured_content is not None and not found.is_error
        top = found.structured_content["results"][0]
        doc = await client.call_tool("read", {"document_id": top["document_id"], "max_chars": 500})
        assert doc.structured_content is not None and not doc.is_error
        return {"top": top, "doc": doc.structured_content}

    for live_server in _serve(settings, db):
        out = with_client(live_server, work)
        calls = live_server.calls()

    top, doc = out["top"], out["doc"]
    assert top["source_id"] == "zim:wikipedia_en_ray-charles_nopic" and top["level"] == 1
    assert top["kiwix_url"].startswith(f"{kiwix_url}/content/wikipedia_en_ray-charles_nopic_")
    assert doc["title"] == top["title"] and doc["text"]
    assert [c.tool for c in calls] == ["search", "read"] and all(c.error is None for c in calls)


def test_erro_de_dados_do_postgres_nao_mostra_sql() -> None:
    from mcp.server.mcpserver.exceptions import ToolError
    from sqlalchemy.exc import DataError

    from ariadne.mcp.server import _tool_errors

    with pytest.raises(ToolError) as excinfo, _tool_errors():
        raise DataError("INSERT INTO query_log ...", {"query": "x"}, Exception("NUL"))

    assert "INSERT" not in str(excinfo.value) and "Invalid argument" in str(excinfo.value)
