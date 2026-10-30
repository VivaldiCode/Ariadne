"""O agente HTTP do Calibre (`deploy/calibre/agent.py`) com o `calibredb` trocado por um doble."""

import importlib.util
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import httpx
import pytest

AGENT = Path(__file__).resolve().parents[2] / "deploy" / "calibre" / "agent.py"


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[ModuleType, str]]:
    spec = importlib.util.spec_from_file_location("calibre_agent", AGENT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "API_KEY", "chave")
    monkeypatch.setattr(module, "VERSION", "calibredb (calibre 9.9)")
    calls: list[list[str]] = []

    def fake_run(args: list[str], timeout: int = 0) -> str:
        calls.append(args)
        raise AssertionError(f"o teste não devia correr {args}")

    monkeypatch.setattr(module, "run", fake_run)
    server = ThreadingHTTPServer(("localhost", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield module, f"http://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_healthz_nao_espera_pelo_lock_nem_corre_nada(agent: tuple[ModuleType, str]) -> None:
    module, url = agent
    with module.LOCK:  # um `calibredb` demorado (uma conversão) tem o LOCK
        response = httpx.get(f"{url}/healthz", timeout=3)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "calibre": "calibredb (calibre 9.9)"}


def test_pedidos_sem_chave_sao_recusados_e_ha_limite_de_tempo_por_leitura(
    agent: tuple[ModuleType, str],
) -> None:
    module, url = agent

    assert module.Handler.timeout == 30
    assert httpx.get(f"{url}/books", timeout=3).status_code == 401
    assert httpx.post(f"{url}/books?filename=x.epub", content=b"abc", timeout=3).status_code == 401


def test_o_upload_le_o_corpo_antes_de_apanhar_o_lock(
    agent: tuple[ModuleType, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, url = agent
    seen: dict[str, bool] = {}

    def fake_add(name: str, path: Path, title: str = "", authors: str = "") -> dict[str, object]:
        seen["body_on_disk"] = path.read_bytes() == b"conteudo"
        seen["locked"] = module.LOCK.locked()
        return {"id": 1}

    monkeypatch.setattr(module, "ensure_library", lambda: None)
    monkeypatch.setattr(module, "add_book", fake_add)

    response = httpx.post(
        f"{url}/books?filename=x.epub",
        content=b"conteudo",
        headers={"X-API-KEY": "chave"},
        timeout=5,
    )

    assert response.status_code == 200 and seen == {"body_on_disk": True, "locked": True}
