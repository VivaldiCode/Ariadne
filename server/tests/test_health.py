from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ariadne.api import health
from ariadne.app import create_app
from ariadne.config import Settings
from ariadne.db.migrate import head_revision

pytestmark = pytest.mark.db

UNREACHABLE_DB = "postgresql+psycopg://ariadne:x@localhost:1/ariadne"


def _get_health(database_url: str) -> httpx.Response:
    app = create_app(Settings(database_url=database_url))
    with TestClient(app) as client:
        return client.get("/health")


@pytest.fixture
def kiwix_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200)

    monkeypatch.setattr(health.httpx, "get", fake_get)


@pytest.mark.usefixtures("kiwix_up")
def test_health_ok(migrated_database_url: str) -> None:
    resp = _get_health(migrated_database_url)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["pgvector"]  # extensão criada pela migração
    assert body["checks"]["schema_revision"] == head_revision()
    assert body["checks"]["kiwix"] == "ok"


def test_health_degradado_sem_kiwix(migrated_database_url: str) -> None:
    app = create_app(Settings(database_url=migrated_database_url, kiwix_url="http://localhost:1"))
    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
    assert resp.json()["checks"]["kiwix"].startswith("unavailable")


@pytest.mark.usefixtures("kiwix_up")
def test_health_503_sem_base_de_dados() -> None:
    resp = _get_health(UNREACHABLE_DB)

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert body["checks"]["database"].startswith("error")


@pytest.fixture
def servicos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Kiwix, Jobe e Moodle de mentira; devolve a lista dos pedidos feitos (URL, cabeçalhos)."""
    health.clear_service_checks()
    pedidos: list[tuple[str, dict[str, Any]]] = []

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        pedidos.append((url, kwargs.get("headers") or {}))
        return httpx.Response(200)

    monkeypatch.setattr(health.httpx, "get", fake_get)
    yield pedidos  # type: ignore[misc]
    health.clear_service_checks()


def _com_cursos(database_url: str) -> TestClient:
    settings = Settings(
        database_url=database_url,
        jobe_url="http://jobe-gateway",
        jobe_api_key="segredo-de-teste",
        moodle_url="http://moodle",
    )
    return TestClient(create_app(settings))


def test_health_jobe_e_moodle_em_cache(
    migrated_database_url: str, servicos: list[tuple[str, dict[str, Any]]]
) -> None:
    with _com_cursos(migrated_database_url) as client:
        for _ in range(5):
            body = client.get("/health").json()

    assert body["checks"]["jobe"] == "ok"
    assert body["checks"]["moodle"] == "ok"
    jobe = [p for p in servicos if "jobe-gateway" in p[0]]
    moodle = [p for p in servicos if "moodle" in p[0]]
    assert len(jobe) == 1 and len(moodle) == 1  # 5 chamadas ao /health, 1 pedido a cada


def test_health_jobe_envia_a_chave_de_api(
    migrated_database_url: str, servicos: list[tuple[str, dict[str, Any]]]
) -> None:
    with _com_cursos(migrated_database_url) as client:
        client.get("/health")

    (headers,) = [h for url, h in servicos if "jobe-gateway" in url]
    assert headers == {"X-API-KEY": "segredo-de-teste"}


def test_health_cache_expira(
    migrated_database_url: str,
    servicos: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agora = [1000.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: agora[0])
    with _com_cursos(migrated_database_url) as client:
        client.get("/health")
        agora[0] += health.SERVICE_CHECK_TTL_SECONDS + 1
        client.get("/health")

    assert len([p for p in servicos if "jobe-gateway" in p[0]]) == 2
