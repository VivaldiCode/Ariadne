"""Fase 7: `translate` (MCP) e `POST /api/translate`."""

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.config import Settings
from ariadne.db.models import ToolCall
from ariadne.translate.fake import FakeTranslator
from tests.test_mcp import Live, _serve, with_client

pytestmark = pytest.mark.db

TEXT = "Veja o exemplo: a função `get_settings()` em https://x.org/a e [doc:zim:a/B] para o teste."


def settings_for(database_url: str, zims: Path) -> Settings:
    return Settings(
        database_url=database_url,
        zim_dir=zims,
        kiwix_url="http://kiwix.invalido",
        kiwix_public_url="http://kiwix.publico:8801",
    )


@pytest.fixture
def translator() -> FakeTranslator:
    return FakeTranslator()


@pytest.fixture
def live(
    database_url: str, db: sessionmaker[Session], zim_dir: Path, translator: FakeTranslator
) -> Iterator[Live]:
    for server in _serve(settings_for(database_url, zim_dir), db):
        server.services.translator = translator
        yield server


@pytest.fixture
def api(
    database_url: str, db: sessionmaker[Session], zim_dir: Path, translator: FakeTranslator
) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, key = create_principal(session, PrincipalKind.user, "pessoa", Role.admin)
    with TestClient(
        create_app(settings_for(database_url, zim_dir)), headers={"Authorization": f"Bearer {key}"}
    ) as client:
        client.app.state.services.translator = translator  # type: ignore[attr-defined]
        yield client


def mcp_translate(live: Live, **arguments: Any) -> tuple[bool, dict[str, Any], str]:
    async def work(client: Client) -> tuple[bool, dict[str, Any], str]:
        result = await client.call_tool("translate", arguments)
        return result.is_error, dict(result.structured_content or {}), str(result.content)

    return with_client(live, work)


def test_mcp_translate_mantem_codigo_citacoes_e_urls(live: Live) -> None:
    is_error, body, _ = mcp_translate(live, text=TEXT, target="en")

    assert not is_error
    assert body["translated"] is True and body["target"] == "en" and body["source"] == "pt"
    assert "`get_settings()`" in body["text"] and "https://x.org/a" in body["text"]
    assert "[doc:zim:a/B]" in body["text"]
    assert body["text"].startswith("See the example:") and body["warning"] is None
    [call] = [c for c in live.calls() if c.tool == "translate"]
    assert call.arguments == {"target": "en", "source": None, "chars": len(TEXT)}
    assert call.result is not None and call.result["translated"] is True
    assert "exemplo" not in str(call.arguments) + str(call.result)  # o texto não fica registado


def test_mcp_translate_degrada_sem_erro(live: Live, translator: FakeTranslator) -> None:
    translator.down = True

    is_error, body, _ = mcp_translate(live, text=TEXT, target="en", source="pt")

    assert not is_error
    assert (body["translated"], body["text"]) == (False, TEXT)
    assert "unavailable" in body["warning"]


def test_mcp_translate_sem_servico_configurado(live: Live) -> None:
    live.services.translator = None
    live.services._translation = None  # type: ignore[attr-defined]

    is_error, body, _ = mcp_translate(live, text="Olá", target="en", source="pt")

    assert not is_error and body["translated"] is False
    assert "LIBRETRANSLATE_URL" in body["warning"]


def test_mcp_translate_erros_para_o_agente_ler(live: Live) -> None:
    is_error, _, text = mcp_translate(live, text="Olá", target="português")
    assert is_error and "Invalid language code" in text
    is_error, _, text = mcp_translate(live, text="   ", target="en")
    assert is_error and "empty" in text
    assert [c.error is not None for c in live.calls() if c.tool == "translate"] == [True, True]


def test_rest_translate(api: TestClient, translator: FakeTranslator) -> None:
    resp = api.post("/api/translate", json={"text": TEXT, "target": "en"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["translated"] and body["text"].startswith("See the example:")
    assert "`get_settings()`" in body["text"] and body["cached"] is False
    again = api.post("/api/translate", json={"text": TEXT, "target": "en"}).json()
    assert again["cached"] is True and len(translator.calls) == 1
    sessions: sessionmaker[Session] = api.app.state.services.sessions  # type: ignore[attr-defined]
    with sessions() as session:
        calls = list(session.scalars(select(ToolCall).where(ToolCall.tool == "translate")))
    assert len(calls) == 2 and calls[0].arguments["chars"] == len(TEXT)


def test_rest_translate_erros(api: TestClient) -> None:
    assert api.post("/api/translate", json={"target": "en"}).status_code == 422
    assert api.post("/api/translate", json={"text": "x", "target": "xx!"}).status_code == 400
    assert api.post("/api/translate", json={"text": "", "target": "en"}).status_code == 400
    big = {"text": "a" * 20_001, "target": "en"}  # TRANSLATE_MAX_CHARS está no esquema do pedido
    assert api.post("/api/translate", json=big).status_code == 422
    api.headers.pop("Authorization")
    assert api.post("/api/translate", json={"text": "x", "target": "en"}).status_code == 401


def test_mcp_translate_tem_o_limite_no_esquema_e_recusa_textos_grandes(live: Live) -> None:
    async def schema(client: Client) -> dict[str, Any]:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        return dict(tools["translate"].input_schema["properties"]["text"])

    assert with_client(live, schema)["maxLength"] == 20_000
    is_error, _, message = mcp_translate(live, text="a" * 20_001, target="en")
    assert is_error and "20000" in message


class Blocking(FakeTranslator):
    """Um tradutor que fica à espera, para haver traduções em curso ao mesmo tempo."""

    def __init__(self, db: sessionmaker[Session]) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.db = db
        self.open_transactions: list[int] = []

    def translate(
        self, text: str, source: str, target: str, *, timeout: float | None = None
    ) -> str:
        with self.db() as other:  # ligações à espera dentro de uma transação, durante a tradução
            self.open_transactions.append(
                other.scalar(
                    text_(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND state = 'idle in transaction'"
                    )
                )
                or 0
            )
        self.started.set()
        self.release.wait(10)
        return super().translate(text, source, target, timeout=timeout)


text_ = text


def test_rest_translate_nao_segura_a_ligacao_e_limita_a_concorrencia(
    database_url: str, db: sessionmaker[Session], zim_dir: Path
) -> None:
    with db() as session, session.begin():
        _, key = create_principal(session, PrincipalKind.user, "pessoa", Role.admin)
    settings = settings_for(database_url, zim_dir)
    settings.translate_concurrency = 1
    settings.translate_timeout_seconds = 0.5  # quem espera a vez desiste depressa
    blocking = Blocking(db)
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {key}"}) as client:
        client.app.state.services.translator = blocking  # type: ignore[attr-defined]
        first: dict[str, Any] = {}

        def slow() -> None:
            first["response"] = client.post(
                "/api/translate", json={"text": "Olá", "target": "en", "source": "pt"}
            )

        thread = threading.Thread(target=slow)
        thread.start()
        assert blocking.started.wait(10)

        busy = client.post(
            "/api/translate", json={"text": "Outra frase", "target": "en", "source": "pt"}
        )

        blocking.release.set()
        thread.join(10)
    assert busy.status_code == 429 and "try again shortly" in busy.json()["detail"]
    assert first["response"].status_code == 200
    assert blocking.open_transactions == [0]  # a sessão do pedido já tinha libertado a ligação
