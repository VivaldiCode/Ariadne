"""A API de admin para o portal comercial: suspender e retomar, criação idempotente, uso por
chave e quota mensal. Tudo só de administradores; as chaves `mcp` nunca chegam a estas rotas."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal, hash_api_key
from ariadne.client_usage import month_bounds
from ariadne.config import Settings
from ariadne.db.models import Principal, ToolCall
from ariadne.mcp.server import stdio_caller
from tests.fakes import default_fake

pytestmark = pytest.mark.db

TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def api(database_url: str, db: sessionmaker[Session], zim_dir: Path) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, admin = create_principal(session, PrincipalKind.user, "admin", Role.admin)
        _, member = create_principal(session, PrincipalKind.user, "membro")
        _, agent = create_principal(session, PrincipalKind.agent, "agente")
    settings = Settings(database_url=database_url, zim_dir=zim_dir, kiwix_url="http://k.invalido")
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {admin}"}) as client:
        client.app.state.services.kiwix = default_fake().client()  # type: ignore[attr-defined]
        client.member, client.agent = member, agent  # type: ignore[attr-defined]
        yield client


def as_(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def new_client(api: TestClient, name: str = "k_cliente", **extra: object) -> dict[str, object]:
    resp = api.post(
        "/api/principals", json={"name": name, "kind": "agent", "scope": "mcp", **extra}
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, object] = resp.json()
    return body


def mcp_post(api: TestClient, key: str) -> object:
    return api.post("/mcp", headers={**as_(key), **MCP_HEADERS}, json=TOOLS_LIST)


def add_calls(db: sessionmaker[Session], principal_id: int, *calls: dict[str, object]) -> None:
    with db() as session, session.begin():
        for call in calls:
            session.add(ToolCall(principal_id=principal_id, arguments={}, **call))  # type: ignore[arg-type]


def now_minus(**delta: float) -> datetime:
    return datetime.now(UTC) - timedelta(**delta)


# --- só administradores -------------------------------------------------------------------------


def test_as_rotas_novas_so_para_administradores(api: TestClient) -> None:
    client = new_client(api)
    cid = client["id"]
    rotas = [
        ("POST", f"/api/principals/{cid}/suspend", None),
        ("POST", f"/api/principals/{cid}/resume", None),
        ("GET", "/api/principals/usage", None),
        ("GET", f"/api/principals/usage?ids={cid}", None),
        ("GET", f"/api/principals/{cid}/usage", None),
        ("PATCH", f"/api/principals/{cid}", {"monthly_query_quota": 5}),
        ("POST", "/api/principals", {"name": "x", "external_id": "e-1"}),
        ("GET", "/api/principals?external_id=e-1", None),
    ]
    mcp_key = str(client["api_key"])
    for key, esperado in (
        (api.member, 403),  # type: ignore[attr-defined]
        (api.agent, 403),  # type: ignore[attr-defined]
        (mcp_key, 403),  # a chave de um cliente externo
    ):
        for method, path, body in rotas:
            got = api.request(method, path, headers=as_(key), json=body)
            assert got.status_code == esperado, (key[:12], method, path, got.status_code)
    for method, path, body in rotas:  # e sem chave: 401
        got = api.request(method, path, headers={"Authorization": ""}, json=body)
        assert got.status_code == 401, (method, path)
    with api.app.state.sessionmaker() as session:  # type: ignore[attr-defined]
        assert session.get_one(Principal, cid).suspended_at is None  # nada mudou
        assert session.get_one(Principal, cid).monthly_query_quota is None


# --- suspender e retomar ------------------------------------------------------------------------


def test_suspender_e_retomar_mantem_a_mesma_chave(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    client = new_client(api)
    cid, key = client["id"], str(client["api_key"])
    hash_before = hash_api_key(key)
    assert mcp_post(api, key).status_code not in (401, 403, 429)  # type: ignore[attr-defined]

    suspended = api.post(f"/api/principals/{cid}/suspend")
    assert suspended.status_code == 200
    assert suspended.json()["suspended"] is True and suspended.json()["active"] is True
    assert suspended.json()["suspended_at"] is not None
    blocked = mcp_post(api, key)
    assert blocked.status_code == 403  # type: ignore[attr-defined]
    assert "suspended" in blocked.json()["detail"]  # type: ignore[attr-defined]
    assert api.get("/api/whoami", headers=as_(key)).status_code == 403  # REST: sempre 403

    again = api.post(f"/api/principals/{cid}/suspend")  # idempotente
    assert (
        again.status_code == 200
        and again.json()["suspended_at"] == suspended.json()["suspended_at"]
    )

    resumed = api.post(f"/api/principals/{cid}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["suspended"] is False and resumed.json()["suspended_at"] is None
    assert mcp_post(api, key).status_code not in (401, 403, 429)  # type: ignore[attr-defined]
    assert api.post(f"/api/principals/{cid}/resume").status_code == 200  # idempotente
    with db() as session:  # o mesmo valor de chave: o hash não mudou
        assert session.get_one(Principal, cid).api_key_hash == hash_before
        tools = session.scalars(
            select(ToolCall.tool).where(ToolCall.tool.like("principal_%")).order_by(ToolCall.id)
        ).all()
    assert "principal_suspend" in tools and "principal_resume" in tools


def test_suspender_recusa_o_que_nao_e_cliente_ou_ja_foi_revogado(api: TestClient) -> None:
    for path in ("suspend", "resume"):
        assert api.post(f"/api/principals/9999/{path}").status_code == 404
    principals = {p["name"]: p["id"] for p in api.get("/api/principals").json()}
    for name in ("admin", "membro", "agente"):  # as chaves do dono nunca se suspendem
        got = api.post(f"/api/principals/{principals[name]}/suspend")
        assert got.status_code == 409, name
    client = new_client(api)
    assert api.delete(f"/api/principals/{client['id']}").status_code == 200  # revogada
    for path in ("suspend", "resume"):
        assert api.post(f"/api/principals/{client['id']}/{path}").status_code == 409
    assert api.get("/api/whoami").json()["role"] == "admin"  # o admin continua a entrar


def test_suspensa_nao_gasta_o_limite_e_stdio_recusa(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    client = new_client(api)
    api.post(f"/api/principals/{client['id']}/suspend")
    resolve = stdio_caller(db, str(client["api_key"]), "sessao")
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="suspended"):
        resolve(None)  # type: ignore[arg-type]
    api.post(f"/api/principals/{client['id']}/resume")
    assert resolve(None).principal_id == client["id"]  # type: ignore[arg-type]


# --- criação idempotente ------------------------------------------------------------------------


def test_criar_com_external_id_repetido_devolve_o_mesmo_sem_segredo(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    body = {"name": "k_abc", "kind": "agent", "scope": "mcp", "external_id": "acct_1"}
    first = api.post("/api/principals", json=body)
    assert first.status_code == 201 and first.json()["api_key"].startswith("ariadne_")
    assert first.json()["idempotent_replay"] is False
    again = api.post("/api/principals", json=body)
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["api_key"] is None and again.json()["idempotent_replay"] is True
    assert again.json()["external_id"] == "acct_1"
    with db() as session:
        assert session.scalar(select(func.count()).where(Principal.name == "k_abc")) == 1
    found = api.get("/api/principals?external_id=acct_1").json()
    assert [p["id"] for p in found] == [first.json()["id"]]
    assert api.get("/api/principals?external_id=nenhum").json() == []


def test_criar_com_cabecalho_idempotency_key(api: TestClient) -> None:
    body = {"name": "k_h", "kind": "agent", "scope": "mcp"}
    headers = {"Idempotency-Key": "pedido-77"}
    first = api.post("/api/principals", json=body, headers={**api.headers, **headers})
    again = api.post("/api/principals", json=body, headers={**api.headers, **headers})
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"] and again.json()["api_key"] is None
    # O cabeçalho e o corpo são o mesmo identificador.
    via_body = api.post("/api/principals", json={**body, "external_id": "pedido-77"})
    assert via_body.status_code == 200 and via_body.json()["id"] == first.json()["id"]
    clash = api.post(
        "/api/principals",
        json={**body, "external_id": "outro"},
        headers={**api.headers, **headers},
    )
    assert clash.status_code == 400
    # Sem identificador não há idempotência: cada pedido cria o seu.
    plain = [api.post("/api/principals", json={"name": "k_p", "kind": "agent"}) for _ in range(2)]
    assert [p.status_code for p in plain] == [201, 201]


def test_o_identificador_nao_serve_para_outro_cliente(api: TestClient) -> None:
    body = {"name": "k_1", "kind": "agent", "scope": "mcp", "external_id": "acct_9"}
    assert api.post("/api/principals", json=body).status_code == 201
    other = api.post("/api/principals", json={**body, "name": "k_2"})
    assert other.status_code == 409
    other_scope = api.post("/api/principals", json={**body, "scope": "full"})
    assert other_scope.status_code == 409


def test_pedidos_iguais_em_simultaneo_criam_um_so(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    body = {"name": "k_corrida", "kind": "agent", "scope": "mcp", "external_id": "acct_race"}
    headers = dict(api.headers)

    def post(_: int) -> tuple[int, str | None]:
        got = api.post("/api/principals", json=body, headers=headers)
        return got.status_code, got.json().get("api_key")

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(post, range(6)))
    codes = sorted(code for code, _ in results)
    assert codes.count(201) == 1 and set(codes) == {200, 201}, results
    assert sum(1 for _, key in results if key) == 1  # o segredo saiu uma única vez
    with db() as session:
        assert session.scalar(select(func.count()).where(Principal.external_id == "acct_race")) == 1


def test_apagar_o_cliente_tira_o_external_id_do_registo(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    client = new_client(api, "k_rgpd", external_id="acct_rgpd")
    assert api.post(f"/api/principals/{client['id']}/erase").json()["erased"] is True
    with db() as session:
        assert "acct_rgpd" not in str([c.arguments for c in session.scalars(select(ToolCall))])
    again = api.post(  # o identificador volta a estar livre
        "/api/principals",
        json={"name": "k_rgpd", "kind": "agent", "scope": "mcp", "external_id": "acct_rgpd"},
    )
    assert again.status_code == 201


# --- quota mensal -------------------------------------------------------------------------------


def test_quota_mensal_da_429_e_o_patch_levanta(api: TestClient, db: sessionmaker[Session]) -> None:
    client = new_client(api, monthly_query_quota=3)
    cid, key = int(client["id"]), str(client["api_key"])  # type: ignore[call-overload]
    assert client["monthly_query_quota"] == 3
    gasto = {"tool": "search", "result": {"count": 1}}
    add_calls(db, cid, gasto, gasto)
    assert mcp_post(api, key).status_code not in (401, 403, 429)  # type: ignore[attr-defined]

    add_calls(db, cid, gasto)  # a terceira
    over = mcp_post(api, key)
    assert over.status_code == 429  # type: ignore[attr-defined]
    detail = over.json()["detail"]  # type: ignore[attr-defined]
    assert "Monthly request quota exceeded (3 requests per month)" in detail
    _, resets = month_bounds()
    wait = int(over.headers["retry-after"])  # type: ignore[attr-defined]
    assert 1 <= wait <= (resets - datetime.now(UTC)).total_seconds() + 1

    raised = api.patch(f"/api/principals/{cid}", json={"monthly_query_quota": 10})
    assert raised.status_code == 200 and raised.json()["monthly_query_quota"] == 10
    assert mcp_post(api, key).status_code not in (401, 403, 429)  # type: ignore[attr-defined]
    lowered = api.patch(f"/api/principals/{cid}", json={"monthly_query_quota": 1})
    assert lowered.status_code == 200
    assert mcp_post(api, key).status_code == 429  # type: ignore[attr-defined]
    removed = api.patch(f"/api/principals/{cid}", json={"monthly_query_quota": None})
    assert removed.json()["monthly_query_quota"] is None
    assert mcp_post(api, key).status_code not in (401, 403, 429)  # type: ignore[attr-defined]


def test_so_contam_para_a_quota_as_chamadas_do_mes_sem_erro(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    client = new_client(api, monthly_query_quota=2)
    cid, key = int(client["id"]), str(client["api_key"])  # type: ignore[call-overload]
    start, _ = month_bounds()
    add_calls(
        db,
        cid,
        {"tool": "search", "created_at": start - timedelta(seconds=1)},  # do mês passado
        {"tool": "search", "error": "Boom"},  # falhou: não gasta
        {"tool": "read"},
    )
    assert mcp_post(api, key).status_code not in (401, 403, 429)  # type: ignore[attr-defined]
    add_calls(db, cid, {"tool": "read"})
    assert mcp_post(api, key).status_code == 429  # type: ignore[attr-defined]
    # A quota de um cliente não é a de outro, nem trava as chaves do dono.
    other = new_client(api, "k_outro", monthly_query_quota=2)
    assert mcp_post(api, str(other["api_key"])).status_code not in (401, 403, 429)  # type: ignore[attr-defined]
    assert mcp_post(api, api.agent).status_code not in (401, 403, 429)  # type: ignore[attr-defined]


def test_quota_so_para_chaves_mcp_e_valores_validos(api: TestClient) -> None:
    for body in (
        {"name": "x", "kind": "agent", "monthly_query_quota": 5},  # scope full
        {"name": "x", "kind": "user", "role": "admin", "monthly_query_quota": 5},
    ):
        assert api.post("/api/principals", json=body).status_code == 400, body
    for quota in (0, -1, 1.5, "muitas", 3_000_000_000):
        body = {"name": "x", "kind": "agent", "scope": "mcp", "monthly_query_quota": quota}
        assert api.post("/api/principals", json=body).status_code == 422, quota
    admin_id = next(p["id"] for p in api.get("/api/principals").json() if p["name"] == "admin")
    assert (
        api.patch(f"/api/principals/{admin_id}", json={"monthly_query_quota": 5}).status_code == 400
    )
    client = new_client(api)
    cid = client["id"]
    for body in ({}, {"role": None}, {"monthly_query_quota": 0}):
        assert api.patch(f"/api/principals/{cid}", json=body).status_code == 422, body
    assert api.patch("/api/principals/9999", json={"monthly_query_quota": 5}).status_code == 404
    # O papel continua a poder mudar sozinho (o que o PATCH já fazia).
    assert api.patch(f"/api/principals/{admin_id}", json={"role": "member"}).status_code == 409


# --- uso por chave ------------------------------------------------------------------------------


def test_uso_filtrado_por_ids_com_dias_ferramentas_e_ultimo_uso(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    a = new_client(api, "k_a", monthly_query_quota=100)
    b = new_client(api, "k_b")
    c = new_client(api, "k_c")
    aid, bid, cid = int(a["id"]), int(b["id"]), int(c["id"])  # type: ignore[call-overload]
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    add_calls(
        db,
        aid,
        {"tool": "search", "result": {"count": 3}, "created_at": today},
        {"tool": "search", "result": {"count": 0}, "created_at": today},
        {"tool": "read", "created_at": today},
        {"tool": "search", "error": "Boom", "created_at": today - timedelta(days=2)},
        {"tool": "search", "result": {"count": 1}, "created_at": today - timedelta(days=40)},
    )
    add_calls(db, bid, {"tool": "sources", "created_at": today})
    add_calls(db, cid, {"tool": "search", "result": {"count": 2}, "created_at": today})

    got = api.get(f"/api/principals/usage?ids={aid},{bid}&days=7")
    assert got.status_code == 200
    usage = got.json()
    assert [u["principal_id"] for u in usage] == [aid, bid]  # filtrado no servidor: sem o c
    first = usage[0]
    assert (first["name"], first["scope"], first["suspended"], first["revoked"]) == (
        "k_a",
        "mcp",
        False,
        False,
    )
    assert first["days"] == 7 and len(first["daily"]) == 7
    assert (first["tool_calls"], first["errors"]) == (4, 1)
    assert (first["searches"], first["empty_searches"]) == (3, 1)
    by_day = {d["day"]: d for d in first["daily"]}
    hoje = by_day[today.date().isoformat()]
    assert (hoje["tool_calls"], hoje["searches"], hoje["empty_searches"], hoje["errors"]) == (
        3,
        2,
        1,
        0,
    )
    assert by_day[(today - timedelta(days=2)).date().isoformat()]["errors"] == 1
    assert by_day[(today - timedelta(days=1)).date().isoformat()]["tool_calls"] == 0  # com zeros
    assert first["by_tool"] == [
        {"tool": "search", "calls": 3, "errors": 1},
        {"tool": "read", "calls": 1, "errors": 0},
    ]
    assert first["last_used_at"].startswith(today.date().isoformat())
    assert first["month"]["quota"] == 100 and first["month"]["used"] >= 3  # o erro não conta
    assert first["month"]["remaining"] == 100 - first["month"]["used"]
    assert usage[1]["month"]["quota"] is None and usage[1]["month"]["remaining"] is None

    # O último uso é o de sempre, mesmo fora do intervalo pedido.
    old = new_client(api, "k_velho")
    add_calls(db, int(old["id"]), {"tool": "search", "created_at": today - timedelta(days=40)})  # type: ignore[call-overload]
    seen = api.get(f"/api/principals/{old['id']}/usage?days=3").json()
    assert seen["tool_calls"] == 0 and seen["last_used_at"] is not None

    # Sem ids: todos os clientes `mcp`, e nenhuma chave do dono.
    todos = api.get("/api/principals/usage").json()
    assert {u["name"] for u in todos} == {"k_a", "k_b", "k_c", "k_velho"}
    assert all(u["days"] == 30 for u in todos)
    # Um principal que não existe sai da resposta; a rota de um só dá 404.
    assert api.get(f"/api/principals/usage?ids={aid},99999").json()[0]["principal_id"] == aid
    assert api.get("/api/principals/99999/usage").status_code == 404


def test_uso_valida_os_parametros_e_ve_suspensos_e_revogados(api: TestClient) -> None:
    for query in (
        "ids=abc",
        "ids=",
        "ids=1&days=0",
        "ids=1&days=9999",
        "ids=" + ",".join(map(str, range(300))),
    ):
        assert api.get(f"/api/principals/usage?{query}").status_code == 422, query
    client = new_client(api)
    api.post(f"/api/principals/{client['id']}/suspend")
    assert api.get(f"/api/principals/{client['id']}/usage").json()["suspended"] is True
    api.delete(f"/api/principals/{client['id']}")
    assert api.get(f"/api/principals/{client['id']}/usage").json()["revoked"] is True
