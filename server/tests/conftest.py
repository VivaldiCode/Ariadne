"""Testes de integração contra o Postgres do docker compose, sempre numa base temporária.

Ligação: TEST_DATABASE_URL (URL de uma base de administração), ou então POSTGRES_PASSWORD e
POSTGRES_PORT lidos do ambiente ou do .env da raiz do repositório.
"""

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from ariadne.db.migrate import upgrade
from ariadne.db.session import make_engine
from tests.ipfake import ip
from tests.zimfactory import make_zim

pytest_plugins = ["tests.releasekit"]  # as fixtures da release (conteúdo, chaves, serviços)

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class TestEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV, extra="ignore")

    test_database_url: str | None = None
    postgres_password: str = ""
    postgres_port: int = 5432
    test_kiwix_url: str | None = None
    kiwix_port: int = 8801
    wikijs_port: int = 8802
    test_wikijs_url: str | None = None
    wikijs_token: str = ""
    wikijs_admin_email: str = ""
    wikijs_admin_password: str = ""
    test_jobe_url: str | None = None
    jobe_port: int = 8804
    jobe_api_key: str = ""
    jobe_gateway_ip: str = ip(172, 28, 77, 10)
    test_moodle_url: str | None = None
    moodle_port: int = 8805
    moodle_token: str = ""
    moodle_ws_user: str = "ariadnews"
    moodle_ws_password: str = ""
    test_calibre_url: str | None = None
    calibre_port: int = 8807
    calibre_api_key: str = ""
    books_dir: str = "./data/books"
    test_kavita_url: str | None = None
    kavita_port: int = 8806
    kavita_api_key: str = ""
    kavita_library_id: int = 0
    kavita_admin_username: str = "ariadne"
    kavita_admin_password: str = ""


def _admin_url() -> URL:
    env = TestEnv()
    if env.test_database_url:
        return make_url(env.test_database_url)
    return URL.create(
        "postgresql+psycopg",
        username="ariadne",
        password=env.postgres_password,
        host="localhost",
        port=env.postgres_port,
        database="postgres",
    )


def _libpq(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Os testes nunca descarregam nem carregam o modelo real: `Settings()` lê EMBEDDER=fake.
    (O teste de ponta a ponta marcado `model` pede `embedder="onnx"` à mão.)"""
    monkeypatch.setenv("EMBEDDER", "fake")


@pytest.fixture(scope="session")
def admin_url() -> URL:
    url = _admin_url()
    try:
        psycopg.connect(_libpq(url), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"Postgres do compose inacessível em {url.host}:{url.port} ({exc}).\n"
            "Sobe-o com: docker compose up -d postgres",
            pytrace=False,
        )
    return url


@pytest.fixture(scope="session")
def make_database(admin_url: URL) -> Iterator[Callable[[], str]]:
    """Fábrica de bases temporárias 'ariadne_test_<hex>', apagadas no fim da sessão."""
    created: list[str] = []

    def make() -> str:
        name = f"ariadne_test_{secrets.token_hex(4)}"
        with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        return admin_url.set(database=name).render_as_string(hide_password=False)

    yield make

    with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
        for name in created:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def empty_database_url(make_database: Callable[[], str]) -> str:
    return make_database()


@pytest.fixture(scope="session")
def migrated_database_url(make_database: Callable[[], str]) -> str:
    url = make_database()
    upgrade(url)
    return url


@pytest.fixture
def database_url(make_database: Callable[[], str]) -> str:
    """Base de dados migrada só para este teste (isolada de todas as outras)."""
    url = make_database()
    upgrade(url)
    return url


@pytest.fixture
def db(database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(database_url)
    yield sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def zim_dir(tmp_path: Path) -> Path:
    """Pasta com um ZIM minúsculo em português (livro do kiwix: wikipedia_pt_teste_2026-09)."""
    directory = tmp_path / "zim"
    directory.mkdir()
    make_zim(directory / "wikipedia_pt_teste_2026-09.zim")
    return directory


@pytest.fixture(scope="session")
def kiwix_url() -> str:
    """kiwix-serve do compose (ZIM de teste em ./data/zim); falha com instruções se não responde."""
    env = TestEnv()
    url = env.test_kiwix_url or f"http://localhost:{env.kiwix_port}"
    try:
        httpx.get(url, timeout=3).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(
            f"kiwix-serve do compose inacessível em {url} ({type(exc).__name__}).\n"
            "Sobe-o com: deploy/kiwix/get-test-zim.sh && docker compose up -d kiwix",
            pytrace=False,
        )
    return url


@pytest.fixture(scope="session")
def real_zim() -> Path:
    """O ZIM de teste que o kiwix do compose serve (deploy/kiwix/get-test-zim.sh)."""
    found = sorted((ROOT_ENV.parent / "data" / "zim").glob("*.zim"))
    if not found:
        pytest.fail("Falta o ZIM de teste: deploy/kiwix/get-test-zim.sh", pytrace=False)
    return found[0]


def _api_key_ids(backend: Any) -> list[int]:
    data = backend._call("{ authentication { apiKeys { id isRevoked } } }", {})
    return [int(k["id"]) for k in data["authentication"]["apiKeys"] if not k["isRevoked"]]


def _revoke_api_keys(url: str, token: str, keep: set[int]) -> None:
    """Revoga as chaves de API que não estavam lá antes dos testes (a última, a do `token`)."""
    from ariadne.wiki.wikijs import WikiJsBackend

    backend = WikiJsBackend(url, token)
    try:
        mine = sorted(set(_api_key_ids(backend)) - keep)
        for key_id in reversed(mine):  # a mais recente (a nossa, se o token foi criado aqui) no fim
            backend._call(
                "mutation($id: Int!) { authentication { revokeApiKey(id: $id) {"
                " responseResult { succeeded } } } }",
                {"id": key_id},
            )
    finally:
        backend.close()


@pytest.fixture
def short_articles(monkeypatch: pytest.MonkeyPatch) -> None:
    """As fixtures de ZIM têm um ou dois parágrafos curtos: o mínimo de texto do `doctype` (300
    caracteres, abaixo é um `skip`) baixa para 20, para o importador as tratar como artigos."""
    from ariadne.wiki import doctype

    monkeypatch.setattr(doctype, "MIN_TEXT", 20)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Os testes com a fixture `wikijs` precisam do Wiki.js vivo: marcam-se `wikijs`."""
    for item in items:
        if "wikijs" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.wikijs)


@pytest.fixture(scope="session")
def wikijs() -> Iterator[tuple[str, str]]:
    """(URL, chave de API) do Wiki.js do compose. Se o `.env` não tem uma chave que sirva, corre o
    arranque (`ariadne wiki bootstrap`) com o administrador do `.env`; falha com instruções se o
    serviço não responde. No fim revoga as chaves que os testes criaram."""
    from ariadne.wiki.base import WikiError
    from ariadne.wiki.bootstrap import BootstrapError, bootstrap
    from ariadne.wiki.wikijs import WikiJsBackend

    env = TestEnv()
    url = env.test_wikijs_url or f"http://localhost:{env.wikijs_port}"
    try:
        httpx.get(f"{url}/healthz", timeout=3).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(
            f"Wiki.js do compose inacessível em {url} ({type(exc).__name__}).\n"
            "Sobe-o com: docker compose up -d wikijs",
            pytrace=False,
        )
    token = ""
    keep: set[int] = set()
    if env.wikijs_token:
        backend = WikiJsBackend(url, env.wikijs_token)
        try:  # um visitante também pesquisa: só uma chave válida lê a versão do sistema
            backend._call("{ system { info { currentVersion } } }", {})
            keep = set(_api_key_ids(backend))
            token = env.wikijs_token
        except WikiError:
            pass  # chave inválida ou revogada: cria-se uma nova
        finally:
            backend.close()
    if not token:
        try:
            token = bootstrap(url, env.wikijs_admin_email, env.wikijs_admin_password, url)
        except BootstrapError as exc:
            pytest.fail(
                f"O Wiki.js de teste não está preparado: {exc}\n"
                "Define WIKIJS_ADMIN_EMAIL e WIKIJS_ADMIN_PASSWORD no .env e corre: "
                "uv run ariadne wiki bootstrap --env-file ../.env",
                pytrace=False,
            )
        backend = WikiJsBackend(url, token)
        keep = set(_api_key_ids(backend)) - {max(_api_key_ids(backend))}  # só a nossa é nova
        backend.close()
    yield url, token
    _revoke_api_keys(url, token, keep)


@pytest.fixture(scope="session")
def jobe_url() -> str:
    """O Jobe do compose (porta do jobe-gateway); falha com instruções se não responde."""
    env = TestEnv()
    url = env.test_jobe_url or f"http://localhost:{env.jobe_port}"
    try:
        httpx.get(
            f"{url}/jobe/index.php/restapi/languages",
            headers={"X-API-KEY": env.jobe_api_key},
            timeout=5,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(
            f"Jobe do compose inacessível em {url} ({type(exc).__name__}).\n"
            "Sobe-o com: docker compose up -d jobe jobe-gateway",
            pytrace=False,
        )
    return url


@pytest.fixture(scope="session")
def jobe_gateway_ip() -> str:
    """O IP do jobe-gateway na rede do compose (a do Moodle): o de JOBE_GATEWAY_IP, se estiver
    definido (um gateway noutra máquina); senão o que o Docker lhe deu (o compose não fixa a
    sub-rede), pelo `docker inspect` do projeto COMPOSE_PROJECT_NAME (por omissão `ariadne`)."""
    import os
    import subprocess

    if os.environ.get("JOBE_GATEWAY_IP"):
        return os.environ["JOBE_GATEWAY_IP"]
    project = os.environ.get("COMPOSE_PROJECT_NAME") or "ariadne"
    try:
        found = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
             "--filter", "label=com.docker.compose.service=jobe-gateway"],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout.split()  # fmt: skip
        if found:
            address = subprocess.run(
                ["docker", "inspect", "-f",
                 f'{{{{(index .NetworkSettings.Networks "{project}_default").IPAddress}}}}',
                 found[0]],
                capture_output=True, text=True, timeout=20, check=True,
            ).stdout.strip()  # fmt: skip
            if address:
                return address
    except (OSError, subprocess.SubprocessError):
        pass
    return TestEnv().jobe_gateway_ip


@pytest.fixture(scope="session")
def jobe_api_key() -> str:
    """A chave de API do jobe-gateway (JOBE_API_KEY do `.env`)."""
    return TestEnv().jobe_api_key


@pytest.fixture(scope="session")
def moodle_access() -> tuple[str, str]:
    """(URL, token) do Moodle do compose. Se o `.env` não tem o token, pede-o ao Moodle com o
    utilizador do serviço (a mesma coisa que `ariadne moodle bootstrap`); falha com instruções se
    o serviço não responde ou ainda está a instalar."""
    env = TestEnv()
    url = env.test_moodle_url or f"http://localhost:{env.moodle_port}"
    try:
        httpx.get(f"{url}/login/index.php", timeout=10).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(
            f"Moodle do compose inacessível em {url} ({type(exc).__name__}).\n"
            "Sobe-o com: docker compose up -d moodle (a 1.ª vez instala o Moodle: alguns minutos) "
            "e corre: uv run ariadne moodle bootstrap --env-file ../.env",
            pytrace=False,
        )
    token = env.moodle_token
    if not token:
        reply = httpx.post(
            f"{url}/login/token.php",
            data={
                "username": env.moodle_ws_user,
                "password": env.moodle_ws_password,
                "service": "ariadne",
            },
            timeout=30,
        ).json()
        token = str(reply.get("token") or "")
    if not token:
        pytest.fail(
            "O Moodle não deu o token do serviço (MOODLE_WS_PASSWORD no .env?).\n"
            "Corre: uv run ariadne moodle bootstrap --env-file ../.env",
            pytrace=False,
        )
    return url, token


@pytest.fixture(scope="session")
def calibre_access() -> tuple[str, str, Path]:
    """(URL do agente, chave, pasta da biblioteca) do Calibre do compose; falha com instruções se
    o agente não responde."""
    env = TestEnv()
    url = env.test_calibre_url or f"http://localhost:{env.calibre_port}"
    try:
        httpx.get(f"{url}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(
            f"O Calibre do compose está inacessível em {url} ({type(exc).__name__}).\n"
            "Sobe-o com: docker compose up -d calibre (CALIBRE_API_KEY no .env)",
            pytrace=False,
        )
    root = Path(env.books_dir)
    if not root.is_absolute():
        root = ROOT_ENV.parent / root
    return url, env.calibre_api_key, root.resolve()


@pytest.fixture(scope="session")
def kavita_access() -> tuple[str, str, int]:
    """(URL, chave de API, id da biblioteca) do Kavita do compose. Se o `.env` não tem a chave,
    corre o arranque (`ariadne kavita bootstrap`) com o administrador do `.env`; falha com
    instruções se o serviço não responde."""
    from ariadne.books.kavita import KavitaError
    from ariadne.books.kavita import bootstrap as kavita_bootstrap

    env = TestEnv()
    url = env.test_kavita_url or f"http://localhost:{env.kavita_port}"
    try:
        httpx.get(f"{url}/api/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(
            f"O Kavita do compose está inacessível em {url} ({type(exc).__name__}).\n"
            "Sobe-o com: docker compose up -d kavita",
            pytrace=False,
        )
    if env.kavita_api_key and env.kavita_library_id:
        return url, env.kavita_api_key, env.kavita_library_id
    try:
        done = kavita_bootstrap(
            url, env.kavita_admin_username, env.kavita_admin_password, "ariadne@example.invalid"
        )
    except KavitaError as exc:
        pytest.fail(
            f"O Kavita de teste não está preparado: {exc}\n"
            "Define KAVITA_ADMIN_PASSWORD no .env e corre: "
            "uv run ariadne kavita bootstrap --env-file ../.env",
            pytrace=False,
        )
    return url, done.api_key, done.library_id
