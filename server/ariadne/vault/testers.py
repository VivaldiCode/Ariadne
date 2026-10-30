"""«Testar ligação» de cada integração, com a configuração efetiva (cofre + ambiente).

Cada teste faz um pedido curto ao serviço, com a chave quando há, e devolve `ok` e uma frase para
mostrar. Nunca devolve nem regista um valor: as mensagens dizem o que falhou («chave recusada»),
não o que foi enviado.
"""

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ariadne.config import Settings


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # não é uma classe de testes do pytest

    ok: bool
    message: str


def make_client() -> httpx.Client:
    """Os testes trocam-no por um cliente com `httpx.MockTransport`."""
    return httpx.Client(timeout=5.0, follow_redirects=False)


def _get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    with make_client() as client:
        return client.get(url, headers=headers or {})


def _down(exc: Exception) -> TestResult:
    return TestResult(False, f"no response ({type(exc).__name__})")


def _need(settings: Settings, *keys: str) -> TestResult | None:
    missing = [k.upper() for k in keys if not getattr(settings, k)]
    return TestResult(False, "set " + ", ".join(missing) + " first") if missing else None


def _status(response: httpx.Response, ok: str) -> TestResult:
    if response.status_code in (401, 403):
        return TestResult(
            False, f"the service answered but rejected the key (HTTP {response.status_code})"
        )
    if response.status_code >= 400:
        return TestResult(False, f"the service answered HTTP {response.status_code}")
    return TestResult(True, ok)


def test_wikijs(s: Settings) -> TestResult:
    if (bad := _need(s, "wikijs_url", "wikijs_token")) is not None:
        return bad
    try:
        base = s.wikijs_url.rstrip("/")
        up = _get(f"{base}/healthz")
        if up.status_code >= 500:
            return TestResult(False, f"Wiki.js answered HTTP {up.status_code}")
        with make_client() as client:
            response = client.post(
                f"{base}/graphql",
                headers={"Authorization": f"Bearer {s.wikijs_token}"},
                json={"query": "{ pages { list(limit: 1) { id } } }"},
            )
    except httpx.HTTPError as exc:
        return _down(exc)
    if response.status_code in (401, 403) or '"errors"' in response.text[:300]:
        return TestResult(False, "Wiki.js rejected the API key")
    return _status(response, "Wiki.js is answering and the key is accepted")


def test_kiwix(s: Settings) -> TestResult:
    if (bad := _need(s, "kiwix_url")) is not None:
        return bad
    try:
        return _status(_get(s.kiwix_url), "kiwix-serve is answering")
    except httpx.HTTPError as exc:
        return _down(exc)


def test_calibre(s: Settings) -> TestResult:
    if (bad := _need(s, "calibre_url", "calibre_api_key")) is not None:
        return bad
    try:
        base = s.calibre_url.rstrip("/")
        up = _get(f"{base}/healthz")
        if up.status_code != 200:
            return TestResult(False, f"the Calibre agent answered HTTP {up.status_code}")
        return _status(
            _get(f"{base}/books", {"X-API-KEY": s.calibre_api_key}),
            "Calibre is answering and the key is accepted",
        )
    except httpx.HTTPError as exc:
        return _down(exc)


def test_kavita(s: Settings) -> TestResult:
    if (bad := _need(s, "kavita_url", "kavita_api_key")) is not None:
        return bad
    try:
        base = s.kavita_url.rstrip("/")
        if (up := _get(f"{base}/api/health")).status_code != 200:
            return TestResult(False, f"Kavita answered HTTP {up.status_code}")
        with make_client() as client:
            response = client.post(
                f"{base}/api/Plugin/authenticate",
                params={"apiKey": s.kavita_api_key, "pluginName": "ariadne"},
            )
    except httpx.HTTPError as exc:
        return _down(exc)
    if response.status_code != 200 or "token" not in response.text:
        return TestResult(False, "Kavita rejected the API key")
    return TestResult(True, "Kavita is answering and the key is accepted")


def test_moodle(s: Settings) -> TestResult:
    if (bad := _need(s, "moodle_url", "moodle_token")) is not None:
        return bad
    try:
        with make_client() as client:
            response = client.post(
                f"{s.moodle_url.rstrip('/')}/webservice/rest/server.php",
                data={
                    "wstoken": s.moodle_token,
                    # O serviço `ariadne` só tem as funções do plugin (não o get_site_info).
                    "wsfunction": "local_ariadne_ping",
                    "moodlewsrestformat": "json",
                },
            )
    except httpx.HTTPError as exc:
        return _down(exc)
    if response.status_code != 200:
        return TestResult(False, f"Moodle answered HTTP {response.status_code}")
    if '"exception"' in response.text[:400] or '"errorcode"' in response.text[:400]:
        return TestResult(False, "Moodle rejected the token")
    return TestResult(True, "Moodle is answering and the token is accepted")


def test_jobe(s: Settings) -> TestResult:
    if (bad := _need(s, "jobe_url")) is not None:
        return bad
    headers = {"X-API-KEY": s.jobe_api_key} if s.jobe_api_key else {}
    try:
        response = _get(s.jobe_url.rstrip("/") + "/jobe/index.php/restapi/languages", headers)
    except httpx.HTTPError as exc:
        return _down(exc)
    return _status(response, "the Jobe gateway is answering and the key is accepted")


def test_curator(s: Settings) -> TestResult:
    """O servidor do curador (llama-server: o Mac no ambiente, o `curator` no standalone). `/health`
    e `/v1/models` são públicos, por isso a chave só se prova com um pedido a sério: uma conclusão
    de 1 token."""
    if (bad := _need(s, "curator_url")) is not None:
        return bad
    base = s.curator_url.rstrip("/")
    headers = {"Authorization": f"Bearer {s.curator_api_key}"} if s.curator_api_key else {}
    try:
        if (up := _get(f"{base}/health")).status_code >= 500:
            return TestResult(
                False, f"Curator is loading the model or failing (HTTP {up.status_code})"
            )
        with make_client() as client:
            response = client.post(
                f"{base}/v1/chat/completions",
                headers=headers,
                json={
                    "model": s.curator_model or "curator",
                    "max_tokens": 1,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=60.0,
            )
    except httpx.HTTPError as exc:
        return TestResult(False, f"Curator offline or unreachable ({type(exc).__name__})")
    return _status(response, "the curator is answering and the key is accepted")


def test_kaggle(s: Settings) -> TestResult:
    missing = []
    if not s.kaggle_username:
        missing.append("KAGGLE_USERNAME")
    if not (s.kaggle_api_token or s.kaggle_key):
        missing.append("KAGGLE_API_TOKEN (or KAGGLE_KEY)")
    if missing:
        return TestResult(False, "set " + ", ".join(missing) + " first")
    return TestResult(
        True,
        "credentials set (the Kaggle connection is only proven when a batch is sent; "
        "the Curation panel shows the state)",
    )


def test_libretranslate(s: Settings) -> TestResult:
    if (bad := _need(s, "libretranslate_url")) is not None:
        return bad
    try:
        return _status(
            _get(s.libretranslate_url.rstrip("/") + "/languages"), "LibreTranslate is answering"
        )
    except httpx.HTTPError as exc:
        return _down(exc)


def test_embedder(s: Settings) -> TestResult:
    if (bad := _need(s, "embedder_url")) is not None:
        return bad
    try:
        return _status(
            _get(s.embedder_url.rstrip("/") + "/v1/models"), "embeddings server is answering"
        )
    except httpx.HTTPError as exc:
        return _down(exc)


def test_release(s: Settings) -> TestResult:
    if (bad := _need(s, "release_github_repo", "release_github_token")) is not None:
        return bad
    try:
        response = _get(
            f"{s.release_github_api_url.rstrip('/')}/repos/{s.release_github_repo}",
            {
                "Authorization": f"Bearer {s.release_github_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    except httpx.HTTPError as exc:
        return _down(exc)
    if response.status_code == 404:
        return TestResult(False, "repository not found (or the token cannot see it)")
    return _status(response, "GitHub is answering and the token is accepted")


def test_mempalace(s: Settings) -> TestResult:
    if (bad := _need(s, "mempalace_dsn")) is not None:
        return bad
    import psycopg

    try:
        psycopg.connect(s.mempalace_dsn, connect_timeout=3).close()
    except psycopg.Error as exc:
        return TestResult(False, f"cannot connect ({type(exc).__name__})")
    return TestResult(True, "MemPalace Postgres is answering")


TESTERS: dict[str, Callable[[Settings], TestResult]] = {
    "wikijs": test_wikijs,
    "kiwix": test_kiwix,
    "calibre": test_calibre,
    "kavita": test_kavita,
    "moodle": test_moodle,
    "jobe": test_jobe,
    "curator": test_curator,
    "kaggle": test_kaggle,
    "libretranslate": test_libretranslate,
    "embedder": test_embedder,
    "release": test_release,
    "mempalace": test_mempalace,
}


def run_test(integration_id: str, settings: Settings) -> TestResult:
    tester = TESTERS.get(integration_id)
    if tester is None:
        return TestResult(False, "this integration has no connection test")
    try:
        return tester(settings)
    except Exception as exc:  # um teste nunca derruba o pedido nem mostra o que enviou
        return TestResult(False, f"the test failed ({type(exc).__name__})")
