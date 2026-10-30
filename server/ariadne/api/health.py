import threading
import time
from collections.abc import Callable
from typing import Literal

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from ariadne import __version__
from ariadne.wiki.wikijs import check_health

router = APIRouter(tags=["operations"])

# O Jobe e o Moodle são verificações lentas e de serviços partilhados: o resultado vale este tempo,
# para um painel que recarrega o /health (ou um healthcheck apertado) não fazer um pedido síncrono
# de 3 s a cada chamada.
SERVICE_CHECK_TTL_SECONDS = 30.0
_service_checks: dict[tuple[str, str], tuple[float, str]] = {}
_service_checks_lock = threading.Lock()


def clear_service_checks() -> None:
    """Esquece os resultados guardados (os testes e o arranque)."""
    with _service_checks_lock:
        _service_checks.clear()


def _cached_check(kind: str, url: str, check: Callable[[], str]) -> str:
    now = time.monotonic()
    key = (kind, url)
    with _service_checks_lock:
        hit = _service_checks.get(key)
        if hit is not None and now - hit[0] < SERVICE_CHECK_TTL_SECONDS:
            return hit[1]
    result = check()
    with _service_checks_lock:
        _service_checks[key] = (time.monotonic(), result)
    return result


class HealthChecks(BaseModel):
    database: str
    pgvector: str | None = None  # versão da extensão
    schema_revision: str | None = None  # revisão Alembic aplicada
    kiwix: str
    wikijs: str = "not configured"  # informativo: o nível 2 só degrada a pesquisa
    # Cursos (Fase 10): informativos; só degradam o estado se estiverem configurados de propósito.
    jobe: str = "not configured"
    moodle: str = "not configured"
    # Livros (Fase 11): o Calibre (gestão) e o Kavita (leitor); idem, só contam se configurados.
    calibre: str = "not configured"
    kavita: str = "not configured"
    # O armazenamento do nível 3 (SEMANTIC_STORE); se o pedido não pôde ser, diz porquê.
    semantic_store: str = "pgvector"


class Health(BaseModel):
    status: Literal["ok", "degraded", "error"]
    version: str
    checks: HealthChecks


@router.get("/health")
def health(request: Request, response: Response) -> Health:
    """200 if the database responds (with 'degraded' if kiwix fails); 503 otherwise."""
    engine: Engine = request.app.state.engine
    database, pgvector, revision = _check_database(engine)
    services = getattr(request.app.state, "services", None)
    if services is not None:
        services.sync_config()
    settings = services.settings if services is not None else request.app.state.settings
    kiwix = _check_kiwix(settings.kiwix_url)
    wikijs = (
        check_health(settings.wikijs_url, settings.wikijs_token) if settings.wikijs_url else None
    )

    jobe = (
        _cached_check(
            "jobe",
            settings.jobe_url,
            lambda: _check_jobe(settings.jobe_url, settings.jobe_api_key),
        )
        if settings.jobe_url
        else None
    )
    moodle = (
        _cached_check("moodle", settings.moodle_url, lambda: _check_moodle(settings.moodle_url))
        if settings.moodle_url
        else None
    )

    calibre = (
        _cached_check("calibre", settings.calibre_url, lambda: _check_calibre(settings.calibre_url))
        if settings.calibre_url
        else None
    )
    kavita = (
        _cached_check("kavita", settings.kavita_url, lambda: _check_kavita(settings.kavita_url))
        if settings.kavita_url
        else None
    )

    if database != "ok":
        status: Literal["ok", "degraded", "error"] = "error"
        response.status_code = 503
    else:
        status = "ok" if kiwix == "ok" else "degraded"
        if wikijs is not None and settings.wikijs_token and not wikijs.startswith("ok"):
            status = "degraded"  # só se está configurado de propósito (há token)
        if settings.moodle_token and moodle is not None and moodle != "ok":
            status = "degraded"
        if jobe is not None and jobe != "ok":
            status = "degraded"
        if calibre is not None and calibre != "ok":
            status = "degraded"
        if settings.kavita_api_key and kavita is not None and kavita != "ok":
            status = "degraded"
    checks = HealthChecks(
        database=database,
        pgvector=pgvector,
        schema_revision=revision,
        kiwix=kiwix,
        wikijs=wikijs or "not configured",
        jobe=jobe or "not configured",
        moodle=moodle or "not configured",
        calibre=calibre or "not configured",
        kavita=kavita or "not configured",
        semantic_store=_semantic_store(request),
    )
    return Health(status=status, version=__version__, checks=checks)


def _semantic_store(request: Request) -> str:
    services = getattr(request.app.state, "services", None)
    if services is None:
        return "pgvector"
    return (
        f"{services.store_name} ({services.store_note})"
        if services.store_note
        else services.store_name
    )


def _check_database(engine: Engine) -> tuple[str, str | None, str | None]:
    try:
        with engine.connect() as conn:
            pgvector = conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            revision = None
            if conn.scalar(text("SELECT to_regclass('alembic_version')")) is not None:
                revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
    except SQLAlchemyError as exc:
        return f"error: {type(exc).__name__}", None, None
    return "ok", pgvector, revision


def _check_jobe(url: str, api_key: str = "") -> str:
    headers = {"X-API-KEY": api_key} if api_key else {}
    try:
        resp = httpx.get(
            url.rstrip("/") + "/jobe/index.php/restapi/languages", headers=headers, timeout=3.0
        )
    except httpx.HTTPError as exc:
        return f"unavailable: {type(exc).__name__}"
    return "ok" if resp.status_code == 200 else f"http {resp.status_code}"


def _check_moodle(url: str) -> str:
    try:
        resp = httpx.get(url.rstrip("/") + "/login/index.php", timeout=3.0, follow_redirects=False)
    except httpx.HTTPError as exc:
        return f"unavailable: {type(exc).__name__}"
    return "ok" if resp.status_code < 500 else f"http {resp.status_code}"


def _check_calibre(url: str) -> str:
    try:
        resp = httpx.get(url.rstrip("/") + "/healthz", timeout=3.0)
    except httpx.HTTPError as exc:
        return f"unavailable: {type(exc).__name__}"
    return "ok" if resp.status_code == 200 else f"http {resp.status_code}"


def _check_kavita(url: str) -> str:
    try:
        resp = httpx.get(url.rstrip("/") + "/api/health", timeout=3.0)
    except httpx.HTTPError as exc:
        return f"unavailable: {type(exc).__name__}"
    return "ok" if resp.status_code == 200 else f"http {resp.status_code}"


def _check_kiwix(url: str) -> str:
    try:
        resp = httpx.get(url, timeout=2.0)
    except httpx.HTTPError as exc:
        return f"unavailable: {type(exc).__name__}"
    return "ok" if resp.status_code < 500 else f"http {resp.status_code}"
