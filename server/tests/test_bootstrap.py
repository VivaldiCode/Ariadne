"""`ariadne bootstrap` (docs/STANDALONE.md): o que faz sem os serviços reais (Wiki.js simulado) e o
que grava na base (cofre e primeira chave de administrador)."""

import json
import re
import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from ariadne.cli_bootstrap import Bootstrap, Skip, _reachable, _theme_dir
from ariadne.config import Settings
from ariadne.db import models
from ariadne.vault import crypto
from ariadne.wiki.bootstrap import BootstrapError, Theme, _login, configure_site

THEME = Theme(css="body{}", head="<link>", body="<script></script>", logo_url="data:x")


class FakeWiki:
    """Um Wiki.js 2.5 mínimo: o que `configure_site` pergunta e muda."""

    def __init__(self, *, installed: list[str], namespacing: bool, namespaces: list[str]) -> None:
        self.installed = set(installed)
        self.config: dict[str, Any] = {
            "locale": "en",
            "autoUpdate": True,
            "namespacing": namespacing,
            "namespaces": namespaces,
        }
        self.theme: dict[str, Any] = {"theme": "default", "iconset": "mdi", "darkMode": False}
        self.theme.update(tocPosition="left", injectCSS="", injectHead="", injectBody="")
        self.logo = ""
        self.offline = False
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query, var = body["query"], body["variables"]
        ok = {"responseResult": {"succeeded": True, "message": ""}}
        if "downloadLocale" in query:
            # Como o Wiki.js real: responde sempre «sucesso», mesmo quando o job não instala.
            self.calls.append(f"download {var['locale']}")
            if not self.offline:
                self.installed.add(var["locale"])
            return httpx.Response(200, json={"data": {"localization": {"downloadLocale": ok}}})
        if "updateLocale" in query:
            self.calls.append("updateLocale")
            self.config.update(namespacing=var["ns"], namespaces=var["nss"])
            return httpx.Response(200, json={"data": {"localization": {"updateLocale": ok}}})
        if "setConfig" in query:
            self.calls.append("setConfig")
            self.theme.update(injectCSS=var["css"], injectHead=var["head"], darkMode=var["dark"])
            return httpx.Response(200, json={"data": {"theming": {"setConfig": ok}}})
        if "updateConfig" in query:
            self.calls.append("logo")
            self.logo = var["logo"]
            return httpx.Response(200, json={"data": {"site": {"updateConfig": ok}}})
        if "theming" in query:
            data = {"theming": {"config": self.theme}, "site": {"config": {"logoUrl": self.logo}}}
            return httpx.Response(200, json={"data": data})
        codes = ("en", "pt", "fr", "de")
        locales = [{"code": c, "isInstalled": c in self.installed} for c in codes]
        data = {"localization": {"locales": locales, "config": self.config}}
        return httpx.Response(200, json={"data": data})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def site(wiki: FakeWiki, locales: list[str], theme: Theme | None) -> list[str]:
    return configure_site(
        "http://wiki",
        "t",
        locales,
        theme,
        client=wiki.client(),
        install_wait=0.05,
        pause=0,
        backoff=(0, 0),
    )


def test_instancia_nova_fica_com_locales_namespacing_e_tema() -> None:
    wiki = FakeWiki(installed=["en"], namespacing=False, namespaces=[])
    changed = site(wiki, ["en", "pt"], THEME)
    assert wiki.installed == {"en", "pt"}
    assert wiki.config["namespacing"] is True and set(wiki.config["namespaces"]) == {"en", "pt"}
    assert wiki.theme["injectCSS"] == "body{}" and wiki.theme["darkMode"] is True
    assert wiki.logo == "data:x"
    assert changed == [
        "1 locale(s) installed",
        "namespacing with 2 locale(s)",
        "Ariadne theme",
        "logo",
    ]
    # Idempotente: a 2.ª vez não muda nada.
    wiki.calls.clear()
    assert site(wiki, ["en", "pt"], THEME) == []
    assert wiki.calls == []


def test_so_acrescenta_e_nao_desfaz_um_tema_feito_a_mao() -> None:
    wiki = FakeWiki(installed=["en", "pt", "fr"], namespacing=True, namespaces=["en", "fr"])
    wiki.theme["injectCSS"] = "/* o meu */"
    wiki.logo = "https://logo"
    changed = site(wiki, ["en", "pt"], THEME)
    assert changed == ["namespacing with 3 locale(s)"]
    assert wiki.config["namespaces"] == ["en", "fr", "pt"]  # o fr que lá estava fica
    assert wiki.theme["injectCSS"] == "/* o meu */" and wiki.logo == "https://logo"


def test_sem_internet_nenhum_locale_por_instalar_entra_nos_namespaces() -> None:
    """Um locale nos `namespaces` sem estar instalado parte o importador (WIKI-IMPORTACAO 5.1)."""
    wiki = FakeWiki(installed=["en"], namespacing=False, namespaces=[])
    wiki.offline = True
    changed = site(wiki, ["en", "pt", "de"], None)
    assert changed[0].startswith("2 locale(s) left to install (pt, de")
    assert wiki.calls.count("download pt") == 3 and "download de" not in wiki.calls  # pára no 1.º
    assert wiki.config["namespacing"] is True and wiki.config["namespaces"] == ["en"]
    assert "setConfig" not in wiki.calls  # sem tema


def test_um_locale_cortado_pelo_catalogo_instala_se_a_segunda() -> None:
    wiki = FakeWiki(installed=["en"], namespacing=True, namespaces=["en"])
    refused = {"pt"}  # a 1.ª descarga do pt é recusada (rajada); a 2.ª passa
    original = wiki.handler

    def flaky(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "downloadLocale" in body["query"] and body["variables"]["locale"] in refused:
            refused.discard(body["variables"]["locale"])
            ok = {"responseResult": {"succeeded": True, "message": ""}}
            return httpx.Response(200, json={"data": {"localization": {"downloadLocale": ok}}})
        return original(request)

    client = httpx.Client(transport=httpx.MockTransport(flaky))
    changed = configure_site(
        "http://wiki",
        "t",
        ["en", "pt", "de"],
        None,
        client=client,
        install_wait=0.05,
        pause=0,
        backoff=(0, 0),
    )
    assert changed == ["2 locale(s) installed", "namespacing with 3 locale(s)"]


def test_o_tema_aponta_para_o_kiwix_desta_instalacao(tmp_path: Path) -> None:
    folder = _theme_dir()
    assert folder is not None, "deploy/wikijs-theme não encontrado"
    theme = Theme.load(folder, "http://localhost:8801/")
    # Só o kiwix desta instalação: nenhum outro endereço sobra do head.html de origem.
    assert all("localhost:8801" in url for url in re.findall(r'href="([^"]+)"', theme.head))
    assert 'href="http://localhost:8801/skin/ariadne/ariadne.css"' in theme.head
    assert 'href="http://localhost:8801/skin/ariadne/favicon.svg"' in theme.head
    assert theme.css and theme.body and theme.logo_url.startswith("data:")


def test_wikijs_por_configurar_nao_e_erro_de_login() -> None:
    """Antes do setup o /graphql dá 404 em JSON sem `data`: é «por configurar», não um erro."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found", "error": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert _login(client, "http://wiki", "a@b.c", "x") is None


def test_url_vazio_salta_e_host_que_nao_aparece_e_erro() -> None:
    with pytest.raises(Skip):
        _reachable("")
    with pytest.raises(BootstrapError, match="its profile switched off"):
        _reachable("http://servico-que-nao-existe.invalid:3000", wait=0)
    assert _reachable("http://localhost:3000/") == "http://localhost:3000"


def _settings(database_url: str, **extra: Any) -> Settings:
    return Settings(database_url=database_url, **extra)


def test_tokens_vao_para_o_cofre_e_o_admin_cria_se_uma_vez(
    database_url: str, db: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    key = crypto.generate_master_key()
    boot = Bootstrap(_settings(database_url, ariadne_master_key=key), db, 1)
    assert boot.vault_problem is None
    assert boot.keep("moodle_token", "tok-1") == "saved to the vault"
    assert boot.keep("moodle_token", "tok-1") == "already in the vault"
    assert boot.settings.moodle_token == "tok-1"

    assert boot.admin_key() == "admin 'admin' created"
    assert "ariadne_" in capsys.readouterr().out  # a chave aparece uma vez, no log
    assert boot.admin_key() == "an admin already exists"
    with db() as session:
        admins = session.scalar(
            select(func.count())
            .select_from(models.Principal)
            .where(models.Principal.role == "admin")
        )
    assert admins == 1


def test_sem_chave_mestra_mostra_o_token_para_o_env(
    database_url: str, db: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    boot = Bootstrap(_settings(database_url, ariadne_master_key=""), db, 1)
    assert boot.vault_problem
    assert boot.keep("kavita_api_key", "k-123") == "not saved"
    assert "KAVITA_API_KEY=k-123" in capsys.readouterr().out


def test_o_papel_do_gateway_cria_se_e_repete_se_sem_mais_acesso(
    database_url: str, db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.engine import make_url

    from ariadne.db.session import make_engine

    role = f"ariadne_gwboot_{secrets.token_hex(3)}"
    boot = Bootstrap(_settings(database_url, ariadne_master_key=""), db, 1)
    monkeypatch.delenv("GPU_GATEWAY_DB_PASSWORD", raising=False)
    with pytest.raises(Skip):
        boot.gateway_role(role)
    monkeypatch.setenv("GPU_GATEWAY_DB_PASSWORD", "pa'ss-1")  # a aspa tem de ir escapada
    try:
        assert boot.gateway_role(role) == f"role {role} created"
        monkeypatch.setenv("GPU_GATEWAY_DB_PASSWORD", "pass-2")
        assert boot.gateway_role(role) == f"role {role} updated"
        url = make_url(database_url).set(username=role, password="pass-2")
        limited = make_engine(url.render_as_string(hide_password=False))
        with limited.connect() as conn:
            conn.exec_driver_sql("SELECT count(*) FROM batch_run")
        with limited.connect() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql("SELECT count(*) FROM principal")
        limited.dispose()
    finally:
        with db() as session, session.begin():
            session.execute(text(f'DROP OWNED BY "{role}"'))
            session.execute(text(f'DROP ROLE "{role}"'))


def test_o_modelo_do_curador_standalone_passa_na_guarda_de_modelos() -> None:
    from ariadne.models_policy import assert_allowed_model

    lock = Path(__file__).resolve().parents[2] / "deploy" / "curator" / "model.lock"
    values = dict(
        line.split("=", 1)
        for line in lock.read_text().splitlines()
        if "=" in line and line[0] != "#"
    )
    for key in ("MODEL_REPO", "MODEL_ALIAS"):
        assert_allowed_model(values[key].strip('"'), "modelo do curador")
