"""Fase 8: o que a app web precisa da API — estatísticas, principais e chaves, coleções, jobs."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, Scope, create_principal
from ariadne.config import Settings
from ariadne.db.models import (
    Chunk,
    Job,
    Principal,
    QueryLog,
    QueryResult,
    ToolCall,
)
from ariadne.db.models import Document as DocumentRow
from ariadne.retention import ERASED_NAME
from tests.fakes import default_fake

pytestmark = pytest.mark.db

PT = "zim:wikipedia_pt_teste"


class Keys:
    admin: str
    member: str
    agent: str


@pytest.fixture
def api(database_url: str, db: sessionmaker[Session], zim_dir: Path) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, admin = create_principal(session, PrincipalKind.user, "admin", Role.admin)
        _, member = create_principal(session, PrincipalKind.user, "membro")
        _, agent = create_principal(session, PrincipalKind.agent, "agente")
    settings = Settings(database_url=database_url, zim_dir=zim_dir, kiwix_url="http://k.invalido")
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {admin}"}) as client:
        client.app.state.services.kiwix = default_fake().client()  # type: ignore[attr-defined]
        keys = Keys()
        keys.admin, keys.member, keys.agent = admin, member, agent
        client.keys = keys  # type: ignore[attr-defined]
        yield client


def as_(api: TestClient, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def keys(api: TestClient) -> Keys:
    k: Keys = api.keys  # type: ignore[attr-defined]
    return k


# --- papéis ------------------------------------------------------------------------------------


def test_whoami_traz_o_papel(api: TestClient) -> None:
    assert api.get("/api/whoami").json()["role"] == "admin"
    assert api.get("/api/whoami", headers=as_(api, keys(api).member)).json()["role"] == "member"


def test_migracao_promove_os_users_existentes(make_database: object) -> None:
    from collections.abc import Callable

    from sqlalchemy import text

    from ariadne.db.migrate import upgrade
    from ariadne.db.session import make_engine

    assert callable(make_database)
    make: Callable[[], str] = make_database
    url = make()
    upgrade(url, "0007")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO principal (kind, name) VALUES ('user', 'u'), ('agent', 'a')")
        )
    upgrade(url, "0008")
    with engine.connect() as conn:
        roles = dict(conn.execute(text("SELECT name, role FROM principal")).all())
    engine.dispose()
    assert roles == {"u": "admin", "a": "member"}


# --- principais e chaves -----------------------------------------------------------------------


def test_principais_so_para_administradores(api: TestClient) -> None:
    for key in (keys(api).member, keys(api).agent):
        assert api.get("/api/principals", headers=as_(api, key)).status_code == 403
        body = {"name": "x"}
        assert api.post("/api/principals", json=body, headers=as_(api, key)).status_code == 403
    api.headers.pop("Authorization")
    assert api.get("/api/principals").status_code == 401


def test_criar_listar_e_revogar_chave(api: TestClient) -> None:
    resp = api.post("/api/principals", json={"name": "claude-desktop", "kind": "agent"})
    assert resp.status_code == 201
    created = resp.json()
    key = created["api_key"]
    assert key.startswith("ariadne_") and created["active"] and created["role"] == "member"

    assert api.get("/api/whoami", headers=as_(api, key)).json()["name"] == "claude-desktop"
    listed = api.get("/api/principals").json()
    assert [p["name"] for p in listed] == ["admin", "membro", "agente", "claude-desktop"]
    assert all("api_key" not in p for p in listed)  # a chave só se mostra uma vez

    revoked = api.delete(f"/api/principals/{created['id']}").json()
    assert revoked["active"] is False and revoked["revoked_at"] is not None
    assert api.get("/api/whoami", headers=as_(api, key)).status_code == 401
    assert api.delete(f"/api/principals/{created['id']}").status_code == 200  # idempotente
    assert api.delete("/api/principals/9999").status_code == 404


def test_so_um_user_pode_ser_admin(api: TestClient) -> None:
    resp = api.post("/api/principals", json={"name": "bot", "kind": "agent", "role": "admin"})
    assert resp.status_code == 400
    ok = api.post("/api/principals", json={"name": "ana", "kind": "user", "role": "admin"})
    assert ok.status_code == 201 and ok.json()["role"] == "admin"
    assert api.post("/api/principals", json={"name": ""}).status_code == 422


def test_chave_mcp_nao_chega_a_nenhuma_rota_rest(api: TestClient) -> None:
    """As chaves dos clientes externos (âmbito `mcp`) só servem o MCP: nenhuma rota REST, nem as
    de leitura, nem as de administração (secção 14.4)."""
    for body in (
        {"name": "x", "kind": "user", "scope": "mcp"},
        {"name": "x", "kind": "agent", "role": "admin", "scope": "mcp"},
        {"name": "x", "kind": "agent", "scope": "outro"},
    ):
        assert api.post("/api/principals", json=body).status_code in (400, 422), body
    resp = api.post("/api/principals", json={"name": "cliente-1", "kind": "agent", "scope": "mcp"})
    assert resp.status_code == 201 and resp.json()["scope"] == "mcp"
    key = resp.json()["api_key"]
    for method, path in (
        ("GET", "/api/whoami"),
        ("GET", "/api/search?q=capital"),
        ("GET", "/api/sources"),
        ("GET", "/api/jobs"),
        ("GET", "/api/backups"),
        ("GET", "/api/config"),
        ("GET", "/api/principals"),
        ("POST", "/api/principals"),
        ("GET", "/api/integrations"),
        ("GET", "/api/curation"),
        ("GET", "/api/compute"),
    ):
        got = api.request(method, path, headers=as_(api, key), json={"name": "y"})
        assert got.status_code == 403, (method, path, got.status_code)
    assert {p["name"]: p["scope"] for p in api.get("/api/principals").json()}["admin"] == "full"


def test_curadoria_dominios_e_colecoes_so_de_administradores(api: TestClient) -> None:
    """Um `user` membro só lê: as escritas da curadoria (revisão, ligações, origem dos conceitos),
    dos domínios, das coleções e o retry de jobs pedem administrador (decisão de 30/10/2026)."""
    member = as_(api, keys(api).member)
    escritas = [
        (
            "/api/curation/review",
            {"document_id": f"{PT}/Lisboa", "fingerprint": "x", "decision": "approve"},
        ),
        ("/api/curation/review-batch", {"items": []}),
        ("/api/links", {"src_document": f"{PT}/Lisboa", "dst_document": f"{PT}/Portugal"}),
        ("/api/links/1", {"status": "accepted"}),
        ("/api/concepts/1", {"origin_lang": "pt"}),
        ("/api/collections", {"name": "x", "source_id": PT, "paths": ["Lisboa"]}),
        ("/api/jobs/1/retry", None),
    ]
    for path, body in escritas:
        refused = api.post(path, json=body, headers=member)
        assert refused.status_code == 403, path
        assert refused.json()["detail"].startswith(f"POST {path} requires an administrator")
    agent = api.post("/api/collections", json=escritas[5][1], headers=as_(api, keys(api).agent))
    assert (
        agent.json()["detail"]
        == "POST /api/collections is only for 'user' principals (people), not agents."
    )
    assignment = api.put(
        f"/api/domain-assignments/{PT}/Lisboa", json={"domain": "x"}, headers=member
    )
    assert assignment.status_code == 403
    for path in ("/api/curation", "/api/domains", "/api/collections", "/api/jobs"):
        assert api.get(path, headers=member).status_code == 200, path  # a leitura continua
    assert api.post("/api/collections", json=escritas[5][1]).status_code == 201  # o admin escreve


def test_o_ultimo_administrador_nao_se_revoga_nem_despromove(api: TestClient) -> None:
    [admin] = [p for p in api.get("/api/principals").json() if p["role"] == "admin"]
    assert api.delete(f"/api/principals/{admin['id']}").status_code == 409
    assert api.patch(f"/api/principals/{admin['id']}", json={"role": "member"}).status_code == 409
    member = next(p for p in api.get("/api/principals").json() if p["name"] == "membro")
    promoted = api.patch(f"/api/principals/{member['id']}", json={"role": "admin"})
    assert promoted.json()["role"] == "admin"
    # Com dois administradores já se pode revogar um.
    assert api.delete(f"/api/principals/{admin['id']}").status_code == 200


# --- estatísticas ------------------------------------------------------------------------------


def seed_log(db: sessionmaker[Session], principal_id: int) -> None:
    now = datetime.now(UTC)
    with db() as session, session.begin():
        session.add(
            DocumentRow(
                id=f"{PT}/Lisboa",
                source_id=PT,
                path="Lisboa",
                version="v",
                title="Lisboa",
                license_class="open",
                heat=4.0,
            )
        )
        session.flush()
        for days_ago, level, latency in [(0, 3, 100), (0, 1, 300), (1, 3, 200), (1, None, 50)]:
            log = QueryLog(
                principal_id=principal_id,
                query="q",
                level_served=level,
                latency_ms=latency,
                created_at=now - timedelta(days=days_ago, seconds=5),
            )
            session.add(log)
            session.flush()
            if level is not None:
                session.add(
                    QueryResult(
                        query_id=log.id,
                        rank=1,
                        document_id=f"{PT}/Lisboa",
                        level=level,
                        used=level == 3,
                    )
                )
        old = QueryLog(
            principal_id=principal_id,
            query="velha",
            level_served=1,
            latency_ms=10,
            created_at=now - timedelta(days=60),
        )
        session.add(old)


def test_stats_overview(api: TestClient, db: sessionmaker[Session]) -> None:
    with db() as session:
        pid = session.scalar(select(Principal.id).where(Principal.name == "admin"))
    assert pid is not None
    seed_log(db, pid)

    body = api.get("/api/stats/overview", params={"days": 3}).json()

    assert body["days"] == 3 and body["queries"] == 4  # a de há 60 dias fica de fora
    assert body["level3_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert len(body["series"]) == 3
    today, yesterday, before = body["series"][2], body["series"][1], body["series"][0]
    assert (today["queries"], today["level1"], today["level3"]) == (2, 1, 1)
    assert (yesterday["queries"], yesterday["level3"], yesterday["empty"]) == (2, 1, 1)
    assert before["queries"] == 0 and before["p50_ms"] is None  # dias vazios também vêm
    assert today["p50_ms"] == pytest.approx(200.0)
    assert body["latency_p50_ms"] == pytest.approx(150.0)
    assert body["totals"]["documents"] == 1 and body["totals"]["sources"] >= 1
    assert api.get("/api/stats/overview", params={"days": 0}).status_code == 422
    assert api.get("/api/stats/overview", params={"days": 9999}).status_code == 422


def test_stats_overview_sem_consultas(api: TestClient) -> None:
    body = api.get("/api/stats/overview").json()
    assert body["queries"] == 0 and body["level3_rate"] is None
    assert len(body["series"]) == 14


def test_stats_top_documents(api: TestClient, db: sessionmaker[Session]) -> None:
    with db() as session:
        pid = session.scalar(select(Principal.id).where(Principal.name == "admin"))
    assert pid is not None
    seed_log(db, pid)

    hot = api.get("/api/stats/top-documents").json()
    assert [d["document_id"] for d in hot] == [f"{PT}/Lisboa"]
    assert hot[0]["heat"] > 3.9 and hot[0]["shown"] == 3 and hot[0]["used"] == 2

    used = api.get("/api/stats/top-documents", params={"by": "used", "days": 1}).json()
    assert used[0]["used"] == 1  # só o de hoje (o intervalo de 1 dia é hoje)
    assert api.get("/api/stats/top-documents", params={"by": "nope"}).status_code == 422


def test_stats_principals(api: TestClient, db: sessionmaker[Session]) -> None:
    api.get("/api/search", params={"q": "capital"})  # regista query_log e tool_call do admin
    api.get("/api/sources", headers=as_(api, keys(api).agent))

    rows = api.get("/api/stats/principals").json()

    by_name = {r["name"]: r for r in rows}
    assert by_name["admin"]["queries"] == 1 and by_name["admin"]["tool_calls"] >= 1
    assert by_name["agente"]["tool_calls"] == 1 and by_name["agente"]["queries"] == 0
    assert rows[0]["name"] == "admin"  # quem mais usa primeiro
    assert api.get("/api/stats/principals", headers=as_(api, keys(api).agent)).status_code == 403


def test_stats_coverage(api: TestClient, db: sessionmaker[Session]) -> None:
    with db() as session, session.begin():
        for name, wiki in (("Lisboa", 7), ("Portugal", None)):
            session.add(
                DocumentRow(
                    id=f"{PT}/{name}",
                    source_id=PT,
                    path=name,
                    version="v",
                    title=name,
                    license_class="open",
                    wiki_page_id=wiki,
                )
            )
        session.flush()
        session.add(Chunk(document_id=f"{PT}/Lisboa", ord=0, text="x"))

    [row] = [r for r in api.get("/api/stats/coverage").json() if r["source_id"] == PT]

    assert row["documents"] == 2 and row["indexed"] == 1 and row["chunks"] == 1
    assert row["imported"] == 1 and row["articles"] and row["curation"] == {}


# --- fontes, jobs, config, coleções ------------------------------------------------------------


def test_fontes_com_espaco_ocupado(api: TestClient) -> None:
    [source] = api.get("/api/sources").json()
    assert source["size_bytes"] and source["size_bytes"] > 1000


def test_config_so_de_leitura_sem_segredos(api: TestClient) -> None:
    body = api.get("/api/config").json()
    assert body["index_daily_budget"] == 20_000 and body["services"]["curator"] is False
    assert "url" not in str(body).lower()


def make_failed_job(db: sessionmaker[Session], key: str | None = "k") -> int:
    with db() as session, session.begin():
        job = Job(
            kind="index_document",
            payload={"document_id": f"{PT}/Lisboa"},
            status="failed",
            attempts=5,
            last_error="boom",
            dedupe_key=key,
            finished_at=datetime.now(UTC),
        )
        session.add(job)
        session.flush()
        return job.id


def test_jobs_lista_os_falhados_e_repete(api: TestClient, db: sessionmaker[Session]) -> None:
    job_id = make_failed_job(db)

    overview = api.get("/api/jobs").json()
    assert [j["id"] for j in overview["failed"]] == [job_id]
    assert overview["failed"][0]["status"] == "failed" and overview["next"] == []

    retried = api.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "pending"
    assert retried.json()["attempts"] == 0 and retried.json()["last_error"] is None
    assert [j["id"] for j in api.get("/api/jobs").json()["next"]] == [job_id]
    assert api.post(f"/api/jobs/{job_id}/retry").status_code == 409  # já não está falhado
    assert api.post("/api/jobs/99999/retry").status_code == 404
    assert (
        api.post(f"/api/jobs/{job_id}/retry", headers=as_(api, keys(api).agent)).status_code == 403
    )


def test_retry_com_job_igual_pendente(api: TestClient, db: sessionmaker[Session]) -> None:
    job_id = make_failed_job(db, "igual")
    with db() as session, session.begin():
        session.add(Job(kind="index_document", payload={}, status="pending", dedupe_key="igual"))

    assert api.post(f"/api/jobs/{job_id}/retry").status_code == 409
    with db() as session:
        assert session.get(Job, job_id).status == "failed"  # type: ignore[union-attr]


def test_colecao_enfileira_e_guarda(api: TestClient, db: sessionmaker[Session]) -> None:
    body = {
        "name": "Capitais",
        "source_id": PT,
        "paths": ["Lisboa", "Portugal", "# comentário", "Não existe", "Lisboa"],
    }

    resp = api.post("/api/collections", json=body)

    assert resp.status_code == 201
    created = resp.json()
    assert (created["queued"], created["known"], created["entries"]) == (2, 0, 4)
    assert created["ignored"] == ["Não existe"] and created["created_by"] == "admin"
    with db() as session:
        queued = session.scalars(select(Job).where(Job.kind == "index_document")).all()
    assert sorted(j.payload["document_id"] for j in queued) == [f"{PT}/Lisboa", f"{PT}/Portugal"]
    assert all(j.priority == pytest.approx(0.1) for j in queued)

    [listed] = api.get("/api/collections").json()
    assert listed["id"] == created["id"]
    detail = api.get(f"/api/collections/{created['id']}").json()
    assert detail["paths"] == ["Lisboa", "Portugal", "Não existe", "Lisboa"]
    assert api.get("/api/collections/999").status_code == 404


def test_colecao_erros(api: TestClient) -> None:
    unknown = api.post(
        "/api/collections", json={"name": "x", "source_id": "zim:nao", "paths": ["a"]}
    )
    assert unknown.status_code == 404 and "Unknown source" in unknown.json()["detail"]
    assert (
        api.post("/api/collections", json={"name": "x", "source_id": PT, "paths": []}).status_code
        == 422
    )
    only_comments = {"name": "x", "source_id": PT, "paths": ["# nada"]}
    assert api.post("/api/collections", json=only_comments).status_code == 400
    agent = as_(api, keys(api).agent)
    ok = {"name": "x", "source_id": PT, "paths": ["Lisboa"]}
    assert api.post("/api/collections", json=ok, headers=agent).status_code == 403
    assert api.get("/api/collections", headers=agent).status_code == 200


# --- a aplicação web como estáticos ------------------------------------------------------------


def test_estaticos_e_fallback_da_spa(database_url: str, zim_dir: Path, tmp_path: Path) -> None:
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>Ariadne</title>")
    (web / "assets" / "app-abc123.js").write_text("console.log(1)")
    settings = Settings(
        database_url=database_url, zim_dir=zim_dir, kiwix_url="http://k.invalido", web_dir=web
    )

    with TestClient(create_app(settings)) as client:
        home = client.get("/")
        assert home.status_code == 200 and "Ariadne" in home.text
        assert home.headers["cache-control"] == "no-cache"
        deep = client.get("/ler")  # caminho do router do cliente
        assert deep.status_code == 200 and "<title>Ariadne</title>" in deep.text
        asset = client.get("/assets/app-abc123.js")
        assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
        assert client.get("/assets/falta.js").status_code == 404
        # Sem extensão, um caminho em `assets/` que não existe cai no index.html: nunca `immutable`.
        missing = client.get("/assets/falta")
        assert missing.status_code == 200 and "<title>Ariadne</title>" in missing.text
        assert missing.headers["cache-control"] == "no-cache"
        assert deep.headers["cache-control"] == "no-cache"
        # O que é da API, do MCP e do /health não vira index.html.
        assert client.get("/api/nao-existe").status_code == 404
        assert client.get("/api/whoami").status_code == 401
        assert client.get("/health").json()["status"] in ("ok", "degraded")
        assert client.get("/openapi.json").status_code == 200
        assert client.post("/mcp", json={}).status_code == 401


def test_sem_pasta_web_nao_monta_nada(database_url: str, zim_dir: Path, tmp_path: Path) -> None:
    settings = Settings(
        database_url=database_url,
        zim_dir=zim_dir,
        kiwix_url="http://k.invalido",
        web_dir=tmp_path / "nao-existe",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 404


# --- o último administrador, com pedidos em simultâneo -----------------------------------------


def two_admins(api: TestClient) -> tuple[dict[str, object], dict[str, object], str, str]:
    """Deixa só dois administradores com chave (A e B), cada um com a sua."""
    made = []
    for name in ("A", "B"):
        resp = api.post("/api/principals", json={"name": name, "kind": "user", "role": "admin"})
        made.append(resp.json())
    [original] = [p for p in api.get("/api/principals").json() if p["name"] == "admin"]
    assert api.delete(f"/api/principals/{original['id']}").status_code == 200
    a, b = made
    return a, b, a["api_key"], b["api_key"]


def test_lock_consultivo_serializa_a_protecao_do_ultimo_admin(
    api: TestClient, database_url: str
) -> None:
    import threading
    import time

    import psycopg

    from ariadne.api.principals import ADMIN_GUARD_LOCK

    _a, b, key_a, _ = two_admins(api)
    url = database_url.replace("postgresql+psycopg", "postgresql")
    holder = psycopg.connect(url)  # outra ligação segura o lock, como um pedido a meio
    holder.execute("SELECT pg_advisory_xact_lock(%s)", (ADMIN_GUARD_LOCK,))
    result: dict[str, int] = {}

    def revoke() -> None:
        resp = api.delete(f"/api/principals/{b['id']}", headers=as_(api, key_a))
        result["status"] = resp.status_code

    thread = threading.Thread(target=revoke)
    thread.start()
    time.sleep(0.8)
    assert thread.is_alive()  # bloqueado à espera do lock: não decidiu com dados velhos
    holder.rollback()
    holder.close()
    thread.join(10)
    assert result["status"] == 200  # A continua administrador, B pode sair


def test_dois_admins_a_revogarem_se_nao_ficam_a_zero(api: TestClient) -> None:
    import threading

    a, b, key_a, key_b = two_admins(api)
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def revoke(target: dict[str, object], key: str) -> None:
        barrier.wait()
        resp = api.delete(f"/api/principals/{target['id']}", headers=as_(api, key))
        statuses.append(resp.status_code)

    threads = [
        threading.Thread(target=revoke, args=(b, key_a)),
        threading.Thread(target=revoke, args=(a, key_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert 200 in statuses and statuses.count(200) == 1  # o outro: 409 (ou 401 se já saiu)
    with api.app.state.sessionmaker() as session:  # type: ignore[attr-defined]
        active = session.scalars(
            select(Principal).where(Principal.role == "admin", Principal.api_key_hash.is_not(None))
        ).all()
    assert len(active) == 1


# --- estatísticas: custo e acesso --------------------------------------------------------------


def test_totais_das_tabelas_grandes_usam_a_estimativa(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ariadne import stats

    monkeypatch.setattr(stats, "_reltuples", lambda session, table: 1_234_567.0)

    totals = api.get("/api/stats/overview").json()["totals"]

    assert totals["documents"] == totals["chunks"] == totals["chunk_vectors"] == 1_234_567


def test_estatisticas_pesadas_so_para_users(api: TestClient) -> None:
    agent = as_(api, keys(api).agent)
    for url in ("/api/stats/overview", "/api/stats/top-documents", "/api/stats/coverage"):
        assert api.get(url, headers=agent).status_code == 403
        assert api.get(url, headers=as_(api, keys(api).member)).status_code == 200


def test_indices_de_datas_existem(database_url: str) -> None:
    from sqlalchemy import create_engine, inspect

    engine = create_engine(database_url)
    try:
        for table, index in (
            ("query_log", "ix_query_log_created_at"),
            ("tool_call", "ix_tool_call_created_at"),
        ):
            assert index in {i["name"] for i in inspect(engine).get_indexes(table)}
    finally:
        engine.dispose()


# --- coleções: limites de quem não administra --------------------------------------------------


def test_colecoes_limites(api: TestClient) -> None:
    """Criar coleções é só de administradores (curadoria): um `user` membro recebe 403."""
    member = as_(api, keys(api).member)
    base = {"name": "x", "source_id": PT, "paths": ["Lisboa"]}

    for priority in (0.3, 0.9):
        refused = api.post("/api/collections", json={**base, "priority": priority}, headers=member)
        assert refused.status_code == 403
    assert api.post("/api/collections", json={**base, "priority": 0.9}).status_code == 201  # admin

    many = {**base, "paths": [f"T{i}" for i in range(201)]}
    assert api.post("/api/collections", json=many).status_code == 201  # admin: até 2000

    long_entry = {**base, "paths": ["a" * 301]}
    assert api.post("/api/collections", json=long_entry).status_code == 422


def test_colecoes_por_hora_e_principal(
    api: TestClient, db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ariadne.api import collections

    monkeypatch.setattr(collections, "MAX_PER_HOUR", 2)
    with db() as session, session.begin():
        _, other = create_principal(session, PrincipalKind.user, "outra-admin", Role.admin)
    body = {"name": "x", "source_id": PT, "paths": ["Lisboa"]}

    assert [api.post("/api/collections", json=body).status_code for _ in range(3)] == [
        201,
        201,
        429,
    ]
    # o limite é por principal
    assert api.post("/api/collections", json=body, headers=as_(api, other)).status_code == 201


def test_jobs_last_error_so_para_users(api: TestClient, db: sessionmaker[Session]) -> None:
    make_failed_job(db)

    [as_user] = api.get("/api/jobs").json()["failed"]
    [as_agent] = api.get("/api/jobs", headers=as_(api, keys(api).agent)).json()["failed"]

    assert as_user["last_error"] == "boom"
    assert as_agent["last_error"] is None and as_agent["status"] == "failed"


# --- cabeçalhos de segurança -------------------------------------------------------------------

SECURITY = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def test_cabecalhos_de_seguranca_em_todas_as_respostas(api: TestClient) -> None:
    from ariadne.security import APP_CSP

    responses = [
        api.get("/health"),
        api.get("/api/whoami"),
        api.get("/api/whoami", headers={"Authorization": "Bearer errada"}),  # 401
        api.get("/api/nao-existe"),  # 404
        api.get("/openapi.json"),
        api.post("/mcp", json={}),  # o MCP também (401 sem chave)
    ]
    for resp in responses:
        for name, value in SECURITY.items():
            assert resp.headers[name] == value, (resp.url, name)
        assert resp.headers["content-security-policy"] == APP_CSP, resp.url
    assert "frame-ancestors 'none'" in APP_CSP and "default-src 'self'" in APP_CSP


def test_docs_tem_uma_csp_propria_para_o_cdn(api: TestClient) -> None:
    for path in ("/docs", "/redoc"):
        resp = api.get(path)
        assert resp.status_code == 200
        csp = resp.headers["content-security-policy"]
        assert "https://cdn.jsdelivr.net" in csp and "frame-ancestors 'none'" in csp
        assert resp.headers["x-frame-options"] == "DENY"


# --- apagamento de um cliente (RGPD) -----------------------------------------------------------


def _history(db: sessionmaker[Session], principal_id: int) -> None:
    """Uma pesquisa com feedback e duas chamadas, como as que o MCP grava."""
    with db() as session, session.begin():
        query = QueryLog(principal_id=principal_id, query="dados pessoais", level_served=1)
        session.add(query)
        session.flush()
        session.add(QueryResult(query_id=query.id, rank=1, level=1, feedback=1, used=True))
        session.add(
            ToolCall(
                principal_id=principal_id, tool="search", arguments={"q": "x"}, query_id=query.id
            )
        )
        session.add(
            ToolCall(principal_id=principal_id, tool="feedback", arguments={}, query_id=query.id)
        )


def test_apagar_um_cliente_leva_o_principal_e_o_historico(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    created = api.post(
        "/api/principals", json={"name": "cliente-portal-0042", "kind": "agent", "scope": "mcp"}
    ).json()
    client_id, client_key = created["id"], created["api_key"]
    _history(db, client_id)
    with db() as session:
        admin_id = session.scalar(select(Principal.id).where(Principal.name == "admin"))
    assert admin_id is not None
    _history(db, admin_id)  # o do dono não se toca

    erased = api.post(f"/api/principals/{client_id}/erase")
    assert erased.status_code == 200
    assert erased.json() == {
        "principal_id": client_id,
        "erased": True,
        "tool_calls": 2,
        "queries": 1,
        "query_results": 1,
    }
    assert api.get("/api/whoami", headers=as_(api, client_key)).status_code == 401
    with db() as session:
        assert session.get(Principal, client_id) is None
        assert not session.scalars(select(ToolCall).where(ToolCall.principal_id == client_id)).all()
        assert not session.scalars(select(QueryLog).where(QueryLog.principal_id == client_id)).all()
        assert len(session.scalars(select(QueryResult)).all()) == 1  # só o do dono
        assert (
            len(session.scalars(select(QueryLog).where(QueryLog.principal_id == admin_id)).all())
            == 1
        )
        creation = session.scalars(
            select(ToolCall).where(ToolCall.tool == "principal_create")
        ).one()
        assert creation.arguments["name"] == ERASED_NAME  # o nome do cliente sai do registo
        assert "cliente-portal-0042" not in str(
            [c.arguments for c in session.scalars(select(ToolCall))]
        )

    again = api.post(f"/api/principals/{client_id}/erase")  # idempotente
    assert again.status_code == 200 and again.json()["erased"] is False


def test_o_dono_nunca_se_apaga(api: TestClient, db: sessionmaker[Session]) -> None:
    """Só as chaves `mcp` se apagam: um admin, um user membro e um agente `full` dão 409; e só um
    administrador apaga (403 para um membro)."""
    with db() as session, session.begin():
        ids = {p.name: p.id for p in session.scalars(select(Principal))}
        client, _ = create_principal(
            session, PrincipalKind.agent, "cliente", Role.member, Scope.mcp
        )
        client_id = client.id
    for name in ("admin", "membro", "agente"):
        refused = api.post(f"/api/principals/{ids[name]}/erase")
        assert refused.status_code == 409, name
    with db() as session:
        assert session.get(Principal, ids["admin"]) is not None
    member = as_(api, keys(api).member)
    assert api.post(f"/api/principals/{client_id}/erase", headers=member).status_code == 403
    assert api.post(f"/api/principals/{client_id}/erase").json()["erased"] is True
