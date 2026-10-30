"""Cofre das integrações: cifra, chave mestra, precedência cofre > ambiente, recarga sem reiniciar,
API de administração (só administradores, nunca devolve o valor) e auditoria."""

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.config import Settings, get_settings
from ariadne.db.models import IntegrationAudit, IntegrationSetting
from ariadne.services import Services
from ariadne.vault import crypto, testers
from ariadne.vault.admin import InvalidValue, VaultAdmin
from ariadne.vault.config import IntegrationConfig
from ariadne.vault.crypto import MasterKeyInvalid, MasterKeyMissing, MasterKeyWrong, VaultError
from ariadne.vault.registry import FIELDS
from ariadne.vault.store import DbVault

SECRET = "chave-de-teste-a"


@pytest.fixture(autouse=True)
def _sem_integracoes_no_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada teste dá os valores das integrações que quer: os do ambiente de quem corre a suite
    (uma stack com CALIBRE_API_KEY, JOBE_API_KEY… exportadas) não entram nas `Settings`."""
    for field in FIELDS.values():
        monkeypatch.delenv(field.env, raising=False)
    monkeypatch.delenv("ARIADNE_MASTER_KEY", raising=False)


OTHER = "chave-de-teste-b"


# --- cifra (sem base de dados) -------------------------------------------------------------


def test_cifra_e_decifra() -> None:
    key = crypto.parse_master_key(crypto.generate_master_key())
    blob = crypto.encrypt(key, "calibre_api_key", SECRET)
    assert SECRET.encode() not in blob
    assert crypto.decrypt(key, "calibre_api_key", blob) == SECRET
    # nonce novo em cada cifra: o mesmo texto não dá o mesmo resultado
    assert crypto.encrypt(key, "calibre_api_key", SECRET) != blob


def test_chave_mestra_errada_falha_com_mensagem_clara() -> None:
    key = crypto.parse_master_key(crypto.generate_master_key())
    wrong = crypto.parse_master_key(crypto.generate_master_key())
    blob = crypto.encrypt(key, "jobe_api_key", SECRET)
    with pytest.raises(MasterKeyWrong, match=r"ARIADNE_MASTER_KEY.*is not the one"):
        crypto.decrypt(wrong, "jobe_api_key", blob)


def test_o_nome_e_dado_associado() -> None:
    key = crypto.parse_master_key(crypto.generate_master_key())
    blob = crypto.encrypt(key, "jobe_api_key", SECRET)
    with pytest.raises(MasterKeyWrong):  # a mesma chave, noutra linha: não decifra
        crypto.decrypt(key, "calibre_api_key", blob)


def test_formatos_da_chave_mestra() -> None:
    raw = bytes(range(32))
    assert crypto.parse_master_key(raw.hex()) == raw
    import base64

    assert crypto.parse_master_key(base64.b64encode(raw).decode()) == raw
    assert crypto.parse_master_key(base64.urlsafe_b64encode(raw).decode()) == raw
    with pytest.raises(MasterKeyMissing, match="ARIADNE_MASTER_KEY"):
        crypto.parse_master_key("  ")
    with pytest.raises(MasterKeyInvalid, match="32 bytes"):
        crypto.parse_master_key("curta")


def test_hint_so_os_ultimos_quatro_e_so_se_o_valor_for_longo() -> None:
    assert crypto.hint(SECRET) == SECRET[-4:]
    assert crypto.hint("curto") == ""


# --- cofre + precedência ------------------------------------------------------------------

pytestmark_db = pytest.mark.db


def _settings(database_url: str, **extra: object) -> Settings:
    return Settings(
        database_url=database_url,
        ariadne_master_key=extra.pop("ariadne_master_key", crypto.generate_master_key()),  # type: ignore[arg-type]
        vault_cache_seconds=0.0,
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.db
def test_o_cofre_ganha_ao_ambiente_e_sem_cofre_vale_o_ambiente(
    database_url: str, db: sessionmaker[Session]
) -> None:
    base = _settings(database_url, calibre_api_key="chave-de-teste-c", jobe_url="http://j")
    config = IntegrationConfig(base, db)
    assert config.effective().calibre_api_key == "chave-de-teste-c"
    assert config.resolve("calibre_api_key").source == "env"

    VaultAdmin(config).set_value("calibre_api_key", SECRET, "pessoa")
    effective = config.effective()
    assert effective.calibre_api_key == SECRET  # cofre > env
    assert effective.jobe_url == "http://j"  # o resto continua a vir do ambiente
    assert config.resolve("calibre_api_key").source == "vault"
    assert base.calibre_api_key == "chave-de-teste-c"  # a base não muda

    VaultAdmin(config).delete("calibre_api_key", "pessoa")
    assert config.effective().calibre_api_key == "chave-de-teste-c"  # volta ao ambiente


@pytest.mark.db
def test_os_valores_tipados_e_invalidos(database_url: str, db: sessionmaker[Session]) -> None:
    config = IntegrationConfig(_settings(database_url), db)
    admin = VaultAdmin(config)
    admin.set_value("kavita_library_id", "7", "p")
    assert config.effective().kavita_library_id == 7
    with pytest.raises(InvalidValue):
        admin.set_value("kavita_library_id", "abc", "p")
    with pytest.raises(InvalidValue, match="http"):
        admin.set_value("curator_url", "mac.local:8080", "p")
    with pytest.raises(InvalidValue):
        admin.set_value("curator_api_key", "linha1\nlinha2", "p")
    with pytest.raises(InvalidValue):
        admin.set_value("curator_api_key", "   ", "p")


@pytest.mark.db
def test_guardado_cifrado_na_base(database_url: str, db: sessionmaker[Session]) -> None:
    config = IntegrationConfig(_settings(database_url), db)
    VaultAdmin(config).set_value("moodle_token", SECRET, "p")
    with db() as session:
        row = session.get(IntegrationSetting, "moodle_token")
        assert row is not None and SECRET.encode() not in bytes(row.ciphertext)
        dump = session.execute(text("SELECT * FROM integration_setting")).all()
        audit = session.execute(text("SELECT * FROM integration_audit")).all()
    assert SECRET not in json.dumps([[str(c) for c in r] for r in dump + audit])


@pytest.mark.db
def test_chave_mestra_errada_nao_escreve_e_o_ambiente_continua(
    database_url: str, db: sessionmaker[Session]
) -> None:
    good = _settings(database_url, kavita_api_key="chave-de-teste-c")
    VaultAdmin(IntegrationConfig(good, db)).set_value("kavita_api_key", SECRET, "p")

    wrong = _settings(database_url, kavita_api_key="chave-de-teste-c")  # outra chave gerada
    config = IntegrationConfig(wrong, db)
    assert config.effective().kavita_api_key == "chave-de-teste-c"  # só o ambiente
    assert config.problem is not None and "is not the one" in config.problem
    with pytest.raises(MasterKeyWrong):
        VaultAdmin(config).set_value("kavita_api_key", OTHER, "p")
    state = VaultAdmin(config).state()
    assert state.problem and "is not the one" in state.problem

    # a chave certa continua a ler o valor original
    assert IntegrationConfig(good, db).effective().kavita_api_key == SECRET


@pytest.mark.db
def test_sem_chave_mestra_o_cofre_nao_escreve_mas_o_ambiente_serve(
    database_url: str, db: sessionmaker[Session]
) -> None:
    base = Settings(database_url=database_url, jobe_api_key="chave-de-teste-c")
    config = IntegrationConfig(base, db, ttl=0)
    assert config.effective().jobe_api_key == "chave-de-teste-c"
    with pytest.raises(MasterKeyMissing):
        VaultAdmin(config).set_value("jobe_api_key", SECRET, "p")


@pytest.mark.db
def test_o_cofre_tem_valores_mas_falta_a_chave(
    database_url: str, db: sessionmaker[Session]
) -> None:
    key = crypto.generate_master_key()
    VaultAdmin(IntegrationConfig(_settings(database_url, ariadne_master_key=key), db)).set_value(
        "jobe_api_key", SECRET, "p"
    )
    config = IntegrationConfig(Settings(database_url=database_url, vault_cache_seconds=0), db)
    assert config.effective().jobe_api_key == ""
    assert config.problem is not None and "ARIADNE_MASTER_KEY" in config.problem


@pytest.mark.db
def test_rekey(database_url: str, db: sessionmaker[Session]) -> None:
    old, new = crypto.generate_master_key(), crypto.generate_master_key()
    config = IntegrationConfig(_settings(database_url, ariadne_master_key=old), db)
    VaultAdmin(config).set_value("jobe_api_key", SECRET, "p")
    assert DbVault(db, old).rekey(new, "p") == 1
    assert (
        IntegrationConfig(_settings(database_url, ariadne_master_key=new), db)
        .effective()
        .jobe_api_key
        == SECRET
    )
    with pytest.raises(MasterKeyWrong), db() as session:
        DbVault(db, old).load_all(session)


@pytest.mark.db
def test_chave_perdida_reset_apaga_o_cofre_e_deixa_recomecar(
    database_url: str, db: sessionmaker[Session]
) -> None:
    lost = _settings(database_url, kavita_api_key="chave-de-teste-c")
    VaultAdmin(IntegrationConfig(lost, db)).set_value("kavita_api_key", SECRET, "p")

    new = _settings(database_url, kavita_api_key="chave-de-teste-c")  # a chave perdeu-se
    config = IntegrationConfig(new, db)
    with pytest.raises(MasterKeyWrong):
        VaultAdmin(config).set_value("kavita_api_key", OTHER, "p")

    assert config.vault is not None and config.vault.wipe("cli:t") == 1
    config.invalidate()
    assert config.effective().kavita_api_key == "chave-de-teste-c"
    assert config.problem is None
    VaultAdmin(config).set_value("kavita_api_key", OTHER, "p")  # já deixa escrever
    assert config.effective().kavita_api_key == OTHER
    with db() as session:
        actions = [a.action for a in session.scalars(select(IntegrationAudit))]
    assert "reset" in actions


# --- sem reiniciar: servidor e worker --------------------------------------------------------


@pytest.mark.db
def test_servicos_veem_a_mudanca_e_refazem_o_cliente(
    database_url: str, db: sessionmaker[Session]
) -> None:
    key = crypto.generate_master_key()
    server = _settings(database_url, ariadne_master_key=key, curator_url="http://antigo:1")
    worker = _settings(database_url, ariadne_master_key=key, curator_url="http://antigo:1")
    server_services = Services.open(server)
    worker_services = Services.open(worker)
    try:
        before = worker_services.get_curator()
        assert before is not None and before.url == "http://antigo:1"

        # o servidor (API) escreve; o worker, noutro processo, vê na leitura seguinte
        assert server_services.config is not None
        VaultAdmin(server_services.config).set_value("curator_url", "http://mac:8080", "pessoa")
        VaultAdmin(server_services.config).set_value("curator_api_key", SECRET, "pessoa")
        server_services.sync_config()

        after = worker_services.get_curator()
        assert after is not None and after is not before
        assert after.url == "http://mac:8080"
        assert worker_services.settings.curator_api_key == SECRET
        assert server_services.get_curator().url == "http://mac:8080"  # type: ignore[union-attr]
        # um cliente que não mudou fica
        assert worker_services.get_curator() is after
    finally:
        server_services.close()
        worker_services.close()


# --- importar do ambiente ------------------------------------------------------------------


@pytest.mark.db
def test_import_env(database_url: str, db: sessionmaker[Session]) -> None:
    base = _settings(
        database_url,
        wikijs_token="wiki-token-0123456789",
        calibre_api_key="chave-de-teste-d",
        curator_url="http://mac:8080",
    )
    config = IntegrationConfig(base, db)
    admin = VaultAdmin(config)
    admin.set_value("calibre_api_key", SECRET, "p")  # já no cofre

    dry = admin.import_env("cli:t", dry_run=True)
    assert set(dry["imported"]) == {"WIKIJS_TOKEN", "CURATOR_URL"}
    assert config.resolve("wikijs_token").source == "env"  # nada mudou

    report = admin.import_env("cli:t")
    assert set(report["imported"]) == {"WIKIJS_TOKEN", "CURATOR_URL"}
    assert report["kept"] == ["CALIBRE_API_KEY"]
    assert config.resolve("wikijs_token").source == "vault"
    assert config.resolve("calibre_api_key").value == SECRET  # o do cofre não foi substituído

    again = admin.import_env("cli:t", overwrite=True)
    assert "CALIBRE_API_KEY" in again["imported"]
    assert config.resolve("calibre_api_key").value == "chave-de-teste-d"


# --- API ---------------------------------------------------------------------------------


@pytest.fixture
def api(database_url: str, db: sessionmaker[Session]) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, admin_key = create_principal(session, PrincipalKind.user, "alice", Role.admin)
        _, member_key = create_principal(session, PrincipalKind.user, "membro", Role.member)
        _, agent_key = create_principal(session, PrincipalKind.agent, "agente", Role.admin)
    settings = Settings(
        database_url=database_url,
        ariadne_master_key=crypto.generate_master_key(),
        vault_cache_seconds=0.0,
        wikijs_url="http://wiki",
        wikijs_token="token-do-ambiente-0123456789",
        kiwix_url="http://kiwix.invalido",
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {admin_key}"}) as c:
        c.member = {"Authorization": f"Bearer {member_key}"}  # type: ignore[attr-defined]
        c.agent = {"Authorization": f"Bearer {agent_key}"}  # type: ignore[attr-defined]
        yield c


def _field(body: dict, integration: str, key: str) -> dict:  # type: ignore[type-arg]
    [item] = [i for i in body["integrations"] if i["id"] == integration]
    [field] = [f for f in item["fields"] if f["key"] == key]
    return field  # type: ignore[no-any-return]


@pytest.mark.db
def test_so_administradores(api: TestClient) -> None:
    routes = [
        ("GET", "/api/integrations"),
        ("GET", "/api/integrations/audit"),
        ("PUT", "/api/integrations/fields/jobe_api_key"),
        ("DELETE", "/api/integrations/fields/jobe_api_key"),
        ("POST", "/api/integrations/fields/jobe_api_key/generate"),
        ("POST", "/api/integrations/fields/jobe_api_key/rotate"),
        ("POST", "/api/integrations/jobe/test"),
    ]
    for method, url in routes:
        body = {"value": OTHER} if method in ("PUT", "POST") else None
        assert api.request(method, url, headers=api.member, json=body).status_code == 403, url  # type: ignore[attr-defined]
        assert api.request(method, url, headers=api.agent, json=body).status_code == 403, url  # type: ignore[attr-defined]
        assert (
            api.request(method, url, headers={"Authorization": "Bearer x"}, json=body).status_code
            == 401
        )
        assert api.request(method, url, headers={"Authorization": ""}, json=body).status_code == 401


@pytest.mark.db
def test_a_api_nunca_devolve_o_valor(api: TestClient) -> None:
    put = api.put("/api/integrations/fields/calibre_api_key", json={"value": SECRET})
    assert put.status_code == 204
    api.put("/api/integrations/fields/calibre_url", json={"value": "http://calibre-externo:8807"})

    listing = api.get("/api/integrations")
    assert listing.status_code == 200
    assert SECRET not in listing.text  # nem em claro, nem em parte além dos 4 finais
    assert SECRET[:-4] not in listing.text
    field = _field(listing.json(), "calibre", "calibre_api_key")
    assert field["defined"] is True and field["source"] == "vault"
    assert field["hint"] == SECRET[-4:] and field["value"] is None
    assert field["updated_by"] == "alice" and field["updated_at"]
    # o que não é segredo mostra-se
    assert (
        _field(listing.json(), "calibre", "calibre_url")["value"] == "http://calibre-externo:8807"
    )
    # o segredo vindo do ambiente também só mostra o fim
    env_field = _field(listing.json(), "wikijs", "wikijs_token")
    assert env_field["source"] == "env" and env_field["value"] is None
    assert "token-do-ambiente-0123456789" not in listing.text

    assert SECRET not in api.get("/api/integrations/audit").text
    assert SECRET not in json.dumps(api.get("/health").json())


@pytest.mark.db
def test_apagar_volta_ao_ambiente(api: TestClient) -> None:
    api.put("/api/integrations/fields/wikijs_token", json={"value": SECRET})
    field = _field(api.get("/api/integrations").json(), "wikijs", "wikijs_token")
    assert field["source"] == "vault"
    assert api.delete("/api/integrations/fields/wikijs_token").status_code == 204
    field = _field(api.get("/api/integrations").json(), "wikijs", "wikijs_token")
    assert field["source"] == "env" and field["defined"] is True


@pytest.mark.db
def test_gerar_devolve_uma_vez_e_roda(api: TestClient) -> None:
    first = api.post("/api/integrations/fields/calibre_api_key/generate")
    assert first.status_code == 200
    generated = first.json()["value"]
    assert len(generated) == 48 and first.json()["shown_once"] is True

    listing = api.get("/api/integrations")
    assert generated not in listing.text and generated[:-4] not in listing.text
    field = _field(listing.json(), "calibre", "calibre_api_key")
    assert field["hint"] == generated[-4:] and field["generable"] is True

    rotated = api.post("/api/integrations/fields/calibre_api_key/rotate", json={})
    assert rotated.json()["value"] != generated
    # um segredo que não é nosso não se gera
    assert api.post("/api/integrations/fields/wikijs_token/generate").status_code == 422
    assert api.post("/api/integrations/fields/wikijs_token/rotate", json={}).status_code == 422
    assert (
        api.post("/api/integrations/fields/wikijs_token/rotate", json={"value": OTHER}).status_code
        == 200
    )


@pytest.mark.db
def test_validacao_e_campos_desconhecidos(api: TestClient) -> None:
    assert api.put("/api/integrations/fields/nao_existe", json={"value": "x"}).status_code == 404
    bad = api.put("/api/integrations/fields/curator_url", json={"value": "isto não é um url"})
    assert bad.status_code == 422
    assert api.put("/api/integrations/fields/jobe_api_key", json={"value": " "}).status_code == 422
    assert api.post("/api/integrations/nao_existe/test").status_code == 404


@pytest.mark.db
def test_auditoria_regista_quem_mudou_o_que_sem_o_valor(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    api.put("/api/integrations/fields/moodle_token", json={"value": SECRET})
    api.post("/api/integrations/fields/jobe_api_key/generate")
    api.delete("/api/integrations/fields/moodle_token")
    api.post("/api/integrations/fields/wikijs_token/rotate", json={"value": OTHER})

    log = api.get("/api/integrations/audit").json()
    actions = [(e["action"], e["key"], e["integration"]) for e in log]
    assert actions[:4] == [
        ("rotate", "wikijs_token", "wikijs"),
        ("delete", "moodle_token", "moodle"),
        ("generate", "jobe_api_key", "jobe"),
        ("set", "moodle_token", "moodle"),
    ]
    assert {e["actor"] for e in log} == {"alice"}
    assert all(e["at"] for e in log)
    with db() as session:
        rows = session.scalars(select(IntegrationAudit)).all()
        dump = json.dumps([[str(c) for c in (r.actor, r.action, r.key, r.detail)] for r in rows])
    assert SECRET not in dump and OTHER not in dump


@pytest.mark.db
def test_testar_ligacao(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = request.headers.get("authorization") or request.headers.get("x-api-key")
        if request.url.path.endswith("/v1/chat/completions"):
            ok = key == f"Bearer {SECRET}"
            return httpx.Response(200 if ok else 401, json={"choices": []})
        return httpx.Response(200, json={})  # /health é público

    monkeypatch.setattr(
        testers, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    api.put("/api/integrations/fields/curator_url", json={"value": "http://mac:8080"})
    api.put("/api/integrations/fields/curator_api_key", json={"value": OTHER})
    wrong = api.post("/api/integrations/curator/test").json()
    assert wrong["ok"] is False and "rejected the key" in wrong["message"]

    api.put("/api/integrations/fields/curator_api_key", json={"value": SECRET})
    right = api.post("/api/integrations/curator/test").json()
    assert right["ok"] is True
    assert SECRET not in json.dumps(right)

    monkeypatch.setattr(
        testers,
        "make_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("sem rota"))
            )
        ),
    )
    off = api.post("/api/integrations/curator/test").json()
    assert off["ok"] is False and "Curator offline" in off["message"]
    assert api.post("/api/integrations/privacidade/test").json()["ok"] is False  # sem teste
    calibre = api.post("/api/integrations/calibre/test").json()["message"]
    assert calibre.startswith("set CALIBRE_URL, CALIBRE_API_KEY first")
    assert SECRET not in json.dumps(api.get("/api/integrations/audit").json())


@pytest.mark.db
def test_a_api_muda_o_que_os_servicos_usam(api: TestClient) -> None:
    services: Services = api.app.state.services  # type: ignore[attr-defined]
    assert services.settings.curator_url == ""
    api.put("/api/integrations/fields/curator_url", json={"value": "http://mac:8080"})
    curator = services.get_curator()
    assert curator is not None and curator.url == "http://mac:8080"


# --- CLI ---------------------------------------------------------------------------------


@pytest.mark.db
def test_cli_import_env_e_list(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from ariadne.cli import app

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ARIADNE_MASTER_KEY", crypto.generate_master_key())
    monkeypatch.setenv("JOBE_API_KEY", SECRET)
    monkeypatch.setenv("JOBE_URL", "http://gateway")
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        done = runner.invoke(app, ["secrets", "import-env"])
        assert done.exit_code == 0, done.output
        assert "JOBE_API_KEY" in done.output and SECRET not in done.output
        listing = runner.invoke(app, ["secrets", "list"])
        assert SECRET not in listing.output and "vault" in listing.output
        assert runner.invoke(app, ["secrets", "status"]).exit_code == 0
        audit = runner.invoke(app, ["secrets", "audit"])
        assert "import" in audit.output and SECRET not in audit.output
    finally:
        get_settings.cache_clear()


def test_vault_error_e_a_base() -> None:
    assert issubclass(MasterKeyWrong, VaultError)
