"""Arranque do Wiki.js 2.5: setup numa instância nova e chave de API (`ariadne wiki bootstrap`).

Também endurece os renderizadores (sem HTML cru no Markdown, filtro de HTML ligado).

Idempotente: numa instância por configurar faz o setup (`POST /finalize`) com o administrador
dado; numa já configurada entra com as mesmas credenciais. Nos dois casos liga a API e cria uma
chave nova de acesso total, que se guarda em `WIKIJS_TOKEN`. As chaves antigas não se revogam
(podem estar em uso): revogam-se no Wiki.js, em Administração > API Access.
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

KEY_EXPIRATION = "3y"
_RATE_LIMIT = re.compile(r"try again in (\d+) seconds")
_LOGIN = """mutation($u: String!, $p: String!) { authentication { login(username: $u, password: $p,
  strategy: "local") { responseResult { succeeded message } jwt } } }"""
_API_STATE = """mutation { authentication { setApiState(enabled: true) {
  responseResult { succeeded message } } } }"""
_CREATE_KEY = """mutation($name: String!, $exp: String!) { authentication {
  createApiKey(name: $name, expiration: $exp, fullAccess: true) {
    key responseResult { succeeded message } } } }"""


_RENDERERS = """query { rendering { renderers { isEnabled key
  config { key value } } } }"""
_UPDATE_RENDERERS = """mutation($renderers: [RendererInput]!) { rendering {
  updateRenderers(renderers: $renderers) { responseResult { succeeded message } } } }"""
# renderizador -> (opção, valor exigido): o conteúdo é de fontes externas, por isso o Markdown
# não aceita HTML cru e o filtro de HTML do Wiki.js fica ligado (`htmlSecurity.safeHTML`).
HARDENING = {"markdownCore": ("allowHTML", False), "htmlSecurity": ("safeHTML", True)}


class BootstrapError(Exception):
    """Não foi possível preparar o Wiki.js; a mensagem diz porquê e o que fazer."""


class _SetupPending(BootstrapError):
    """Um Wiki.js por configurar só serve o assistente de setup: o /graphql dá 404."""


def _graphql(
    client: httpx.Client, url: str, query: str, variables: dict[str, Any], jwt: str | None
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {jwt}"} if jwt else {}
    for _ in range(3):
        response = client.post(
            f"{url}/graphql", json={"query": query, "variables": variables}, headers=headers
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise BootstrapError(
                f"Wiki.js answered HTTP {response.status_code} without JSON (still starting?)."
            ) from exc
        if "data" not in body and not body.get("errors"):
            raise _SetupPending(f"Wiki.js answered HTTP {response.status_code} without data.")
        if not body.get("errors"):
            data: dict[str, Any] = body["data"]
            return data
        message = str(body["errors"][0].get("message"))
        limited = _RATE_LIMIT.search(message)  # o Wiki.js limita os logins por minuto
        if limited is None:
            raise BootstrapError("Wiki.js rejected the request: " + message)
        time.sleep(min(int(limited.group(1)) + 1, 90))
    raise BootstrapError(
        "Wiki.js rate-limited the requests (too many logins); try again in a minute."
    )


def _login(client: httpx.Client, url: str, email: str, password: str) -> str | None:
    try:
        data = _graphql(client, url, _LOGIN, {"u": email, "p": password}, None)
    except _SetupPending:
        return None  # por configurar: o `bootstrap` faz o setup
    result = data["authentication"]["login"]
    return str(result["jwt"]) if result["responseResult"]["succeeded"] else None


def harden_renderers(client: httpx.Client, url: str, jwt: str) -> list[str]:
    """Põe `markdownCore.allowHTML=false` e garante `htmlSecurity.safeHTML=true` (e ligado).
    Devolve o que mudou; não faz nada se já estava assim."""
    data = _graphql(client, url, _RENDERERS, {}, jwt)
    changed: list[str] = []
    updates: list[dict[str, Any]] = []
    for renderer in data["rendering"]["renderers"]:
        wanted = HARDENING.get(renderer["key"])
        if wanted is None:
            continue
        option, value = wanted
        config = {c["key"]: json.loads(c["value"]).get("value") for c in renderer["config"]}
        if config.get(option) == value and renderer["isEnabled"]:
            continue
        config[option] = value
        updates.append(
            {
                "isEnabled": True,
                "key": renderer["key"],
                "config": [{"key": k, "value": json.dumps({"v": v})} for k, v in config.items()],
            }
        )
        changed.append(f"{renderer['key']}.{option}={value}")
    if updates:
        result = _graphql(client, url, _UPDATE_RENDERERS, {"renderers": updates}, jwt)
        done = result["rendering"]["updateRenderers"]["responseResult"]
        if not done["succeeded"]:
            raise BootstrapError(f"Could not harden the renderers: {done['message']}")
    return changed


_LOCALES = """{ localization { locales { code isInstalled }
  config { locale autoUpdate namespacing namespaces } } }"""
_DOWNLOAD_LOCALE = """mutation($locale: String!) { localization { downloadLocale(locale: $locale) {
  responseResult { succeeded message } } } }"""
_UPDATE_LOCALE = """mutation($locale: String!, $auto: Boolean!, $ns: Boolean!, $nss: [String]!) {
  localization { updateLocale(locale: $locale, autoUpdate: $auto, namespacing: $ns,
  namespaces: $nss) { responseResult { succeeded message } } } }"""
_THEME = """{ theming { config { theme iconset darkMode tocPosition injectCSS injectHead
  injectBody } } site { config { logoUrl } } }"""
_SET_THEME = """mutation($theme: String!, $iconset: String!, $dark: Boolean!, $toc: String,
  $css: String, $head: String, $body: String) { theming { setConfig(theme: $theme,
  iconset: $iconset, darkMode: $dark, tocPosition: $toc, injectCSS: $css, injectHead: $head,
  injectBody: $body) { responseResult { succeeded message } } } }"""
_SET_LOGO = """mutation($logo: String!) { site { updateConfig(logoUrl: $logo) {
  responseResult { succeeded message } } } }"""
# O catálogo de locales responde 429 ao fim de ~10 descargas seguidas: antes de desistir de um
# locale espera-se isto (segundos) e tenta-se outra vez.
LOCALE_BACKOFF = (30.0, 120.0)
# O head.html do tema liga as fontes e o ícone ao skin do kiwix-serve; o endereço é o do kiwix que o
# navegador vê (KIWIX_PUBLIC_URL), por isso troca-se o de lá pelo desta instalação.
_SKIN_URL = re.compile(r"https?://[^\"'\s]+?/skin/ariadne/")


@dataclass(frozen=True)
class Theme:
    """Os campos de Administração → Tema e a logo (deploy/wikijs-theme)."""

    css: str
    head: str
    body: str
    logo_url: str

    @classmethod
    def load(cls, folder: Path, kiwix_public_url: str) -> "Theme":
        def read(name: str) -> str:
            return (folder / name).read_text(encoding="utf-8").strip()

        skin = kiwix_public_url.rstrip("/") + "/skin/ariadne/"
        return cls(
            css=read("theme.css"),
            head=_SKIN_URL.sub(skin, read("head.html")),
            body=read("body.html"),
            logo_url=read("logo-url.txt"),
        )


def _ok(data: dict[str, Any], *path: str) -> None:
    for key in path:
        data = data[key]
    result = data["responseResult"]
    if not result["succeeded"]:
        raise BootstrapError(f"Wiki.js rejected {path[-1]}: {result['message']}")


def _installed(http: httpx.Client, url: str, token: str) -> tuple[set[str], dict[str, Any]]:
    data = _graphql(http, url, _LOCALES, {}, token)["localization"]
    config: dict[str, Any] = data["config"]
    installed = {str(loc["code"]) for loc in data["locales"] or [] if loc["isInstalled"]}
    installed.add(str(config["locale"] or "en"))
    return installed, config


def configure_site(
    url: str,
    token: str,
    locales: list[str],
    theme: Theme | None = None,
    *,
    client: httpx.Client | None = None,
    install_wait: float = 60.0,
    pause: float = 4.0,
    backoff: tuple[float, ...] = LOCALE_BACKOFF,
) -> list[str]:
    """Deixa uma instância nova pronta para o importador: os `locales` instalados e o *namespacing*
    ligado com eles (o importador publica em `/<locale>/…`), e o tema do Ariadne. Só acrescenta: um
    locale a mais que já lá esteja fica, e o tema só se aplica se o CSS do Wiki.js estiver vazio
    (não desfaz um tema escolhido à mão), a logo idem. Devolve o que mudou.

    O `downloadLocale` responde sempre «sucesso» (o Wiki.js engole os erros do job) e um locale nos
    `namespaces` sem estar instalado parte o importador (docs/WIKI-IMPORT.md, 5.1): por isso
    descarrega-se um a um, com uma pausa (o graph.requarks.io recusa rajadas), confirma-se o
    `isInstalled` e só então entra nos `namespaces`. Se um não se instala (sem internet), os outros
    não se tentam: ficam para a próxima corrida."""
    url = url.rstrip("/")
    http = client or httpx.Client(timeout=120.0)  # descarregar um locale demora
    changed: list[str] = []
    try:
        installed, config = _installed(http, url, token)
        site = str(config["locale"] or "en")
        missing = [locale for locale in locales if locale not in installed]
        added: list[str] = []
        for number, locale in enumerate(missing):
            # O graph.requarks.io corta rajadas (429): pausa entre locales e, se um não se instala,
            # mais tentativas depois das esperas de `backoff` antes de concluir que não há acesso.
            for attempt, wait in enumerate((pause, *backoff)):
                if number or attempt:
                    time.sleep(wait)
                _graphql(http, url, _DOWNLOAD_LOCALE, {"locale": locale}, token)
                deadline = time.monotonic() + install_wait
                while locale not in installed and time.monotonic() < deadline:
                    time.sleep(min(pause, 2.0) or 0.01)
                    installed, config = _installed(http, url, token)
                if locale in installed:
                    break
            if locale not in installed:
                left = missing[number:]
                changed.append(
                    f"{len(left)} locale(s) left to install ({', '.join(left[:5])}…: Wiki.js did "
                    "not "
                    "download them; no access to graph.requarks.io?)"
                )
                break
            added.append(locale)
        if added:
            changed.append(f"{len(added)} locale(s) installed")
        wanted = [locale for locale in locales if locale in installed]
        namespaces = list(dict.fromkeys([*(config["namespaces"] or []), site, *wanted]))
        if not config["namespacing"] or set(namespaces) != set(config["namespaces"] or []):
            variables = {
                "locale": site,
                "auto": bool(config["autoUpdate"]),
                "ns": True,
                "nss": namespaces,
            }
            _ok(_graphql(http, url, _UPDATE_LOCALE, variables, token),
                "localization", "updateLocale")  # fmt: skip
            changed.append(f"namespacing with {len(namespaces)} locale(s)")
        if theme is not None:
            current = _graphql(http, url, _THEME, {}, token)
            look = current["theming"]["config"]
            if not (look.get("injectCSS") or "").strip():
                variables = {
                    "theme": look.get("theme") or "default",
                    "iconset": look.get("iconset") or "mdi",
                    "dark": True,
                    "toc": look.get("tocPosition") or "left",
                    "css": theme.css,
                    "head": theme.head,
                    "body": theme.body,
                }
                _ok(_graphql(http, url, _SET_THEME, variables, token), "theming", "setConfig")
                changed.append("Ariadne theme")
            if not (current["site"]["config"].get("logoUrl") or "").strip():
                _ok(_graphql(http, url, _SET_LOGO, {"logo": theme.logo_url}, token),
                    "site", "updateConfig")  # fmt: skip
                changed.append("logo")
    except httpx.HTTPError as exc:
        raise BootstrapError(f"Wiki.js unreachable at {url}: {type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
    return changed


def _wait_healthy(client: httpx.Client, url: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if client.get(f"{url}/healthz").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise BootstrapError(f"Wiki.js does not answer at {url} (docker compose up -d wikijs).")
        time.sleep(2)


def bootstrap(
    url: str,
    email: str,
    password: str,
    public_url: str,
    *,
    wait_seconds: float = 90.0,
    client: httpx.Client | None = None,
) -> str:
    """Devolve a chave de API nova (para `WIKIJS_TOKEN`)."""
    if not email or not password:
        raise BootstrapError(
            "WIKIJS_ADMIN_EMAIL and WIKIJS_ADMIN_PASSWORD are missing (the Wiki.js administrator)."
        )
    url = url.rstrip("/")
    http = client or httpx.Client(timeout=30.0)
    try:
        _wait_healthy(http, url, wait_seconds)
        jwt = _login(http, url, email, password)
        if jwt is None:  # por configurar? tenta o setup; senão, as credenciais não servem
            # Numa ligação nova: em modo de setup o Wiki.js rebenta a responder ao /graphql (404) e
            # deixa a ligação keep-alive estragada, e o /finalize nela dava ReadError.
            fresh = httpx.Client(timeout=300.0) if client is None else http
            try:
                setup: httpx.Response | None = fresh.post(
                    f"{url}/finalize",
                    json={
                        "adminEmail": email,
                        "adminPassword": password,
                        "adminPasswordConfirm": password,
                        "siteUrl": public_url,
                        "telemetry": False,
                    },
                    timeout=300.0,  # cria as tabelas todas
                )
            except (httpx.ReadError, httpx.RemoteProtocolError):
                # O Wiki.js reinicia-se no fim do setup e às vezes corta a resposta: o login a
                # seguir diz se o setup ficou feito.
                setup = None
            finally:
                if fresh is not http:
                    fresh.close()
            if setup is not None:
                if setup.status_code != 200 or not setup.headers.get("content-type", "").startswith(
                    "application/json"
                ):
                    raise BootstrapError(
                        "Wiki.js is already set up and the given administrator cannot sign in: "
                        "check "
                        "WIKIJS_ADMIN_EMAIL and WIKIJS_ADMIN_PASSWORD."
                    )
                if not setup.json().get("ok"):
                    raise BootstrapError(f"Wiki.js setup failed: {setup.json().get('error')}")
            deadline = time.monotonic() + wait_seconds
            while jwt is None:  # o Wiki.js reinicia-se a si próprio depois do setup
                try:
                    jwt = _login(http, url, email, password)
                except (httpx.HTTPError, ValueError, KeyError, BootstrapError):
                    jwt = None  # a reiniciar: sem resposta, sem JSON ou ainda no setup
                if jwt is None:
                    if time.monotonic() > deadline:
                        raise BootstrapError("Wiki.js was not ready after the setup.")
                    time.sleep(2)
        state = _graphql(http, url, _API_STATE, {}, jwt)["authentication"]["setApiState"]
        if not state["responseResult"]["succeeded"]:
            raise BootstrapError(f"Could not enable the API: {state['responseResult']['message']}")
        harden_renderers(http, url, jwt)
        created = _graphql(
            http,
            url,
            _CREATE_KEY,
            {"name": f"ariadne-{date.today():%Y%m%d}", "exp": KEY_EXPIRATION},
            jwt,
        )["authentication"]["createApiKey"]
        if not created["responseResult"]["succeeded"] or not created["key"]:
            raise BootstrapError(
                f"Could not create the key: {created['responseResult']['message']}"
            )
        return str(created["key"])
    except httpx.HTTPError as exc:
        raise BootstrapError(f"Wiki.js unreachable at {url}: {type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
