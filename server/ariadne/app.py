import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.exc import DataError
from starlette.routing import Route

from ariadne import __version__
from ariadne.api import (
    backups,
    books,
    catalog,
    collections,
    compute,
    concepts,
    courses,
    curation,
    domains,
    health,
    integrations,
    jobs,
    library,
    licenses,
    links,
    principals,
    stats,
    system,
    translate,
    whoami,
)
from ariadne.api.books import BookNotFound
from ariadne.api.courses import CourseNotFound
from ariadne.concepts.origin import ConceptNotFound
from ariadne.config import Settings, get_settings
from ariadne.curation.pipeline import CurationConflict
from ariadne.curation.queue_view import CurationNotFound
from ariadne.documents import (
    INVALID_ARGUMENT,
    DocumentNotFound,
    FeedbackError,
    LibraryError,
    SectionNotFound,
    UnknownSource,
)
from ariadne.graph import LinkNotFound
from ariadne.mcp.server import McpEndpoint, RateLimiter, build_mcp_server, http_caller
from ariadne.search import SearchUnavailable
from ariadne.security import SecurityHeaders
from ariadne.services import Services
from ariadne.translate.service import TranslateBusy
from ariadne.webapp import SpaStaticFiles, web_dir_ready
from ariadne.zimcatalog.opds import CatalogError

logger = logging.getLogger(__name__)

NOT_FOUND = (
    DocumentNotFound,
    UnknownSource,
    SectionNotFound,
    FeedbackError,
    LinkNotFound,
    CurationNotFound,
    ConceptNotFound,
    CourseNotFound,
    BookNotFound,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # um INFO por pedido ao kiwix é ruído

    def current_services() -> Services:
        services: Services = app.state.services
        services.sync_config()  # o cofre das integrações pode ter mudado
        return services

    mcp = build_mcp_server(
        current_services, http_caller, max_translate_chars=settings.translate_max_chars
    )
    # Cria o gestor de sessões do Streamable HTTP; a app Starlette que devolve não se usa porque
    # o endpoint vai direto para as rotas desta app (com a nossa autenticação por chave de API).
    # Sem proteção de DNS rebinding: o acesso é controlado pela chave, e o servidor é para a rede.
    mcp.streamable_http_app(
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = Services.open(settings)
        app.state.engine = services.engine
        app.state.sessionmaker = services.sessions
        app.state.services = services
        await anyio.to_thread.run_sync(services.sync_sources)
        threading.Thread(target=services.warm_embedder, name="warm-embedder", daemon=True).start()
        async with mcp.session_manager.run():
            yield
        services.close()

    app = FastAPI(title="Ariadne", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(LibraryError)
    async def library_error(request: Request, exc: LibraryError) -> JSONResponse:
        status = (
            404
            if isinstance(exc, NOT_FOUND)
            else 409
            if isinstance(exc, CurationConflict)
            else 429
            if isinstance(exc, TranslateBusy)
            else 503
            if isinstance(exc, SearchUnavailable)
            else 400
        )
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.exception_handler(CatalogError)
    async def catalog_error(request: Request, exc: CatalogError) -> JSONResponse:
        # O catálogo do Kiwix é um serviço externo: falhar é 502, não um erro do pedido.
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(DataError)
    async def data_error(request: Request, exc: DataError) -> JSONResponse:
        return JSONResponse({"detail": INVALID_ARGUMENT}, status_code=400)  # sem SQL na resposta

    app.include_router(health.router)
    app.include_router(whoami.router, prefix="/api")
    app.include_router(library.router, prefix="/api")
    app.include_router(links.router, prefix="/api")
    app.include_router(concepts.router, prefix="/api")
    app.include_router(translate.build_router(settings.translate_max_chars), prefix="/api")
    app.include_router(curation.router, prefix="/api")
    app.include_router(compute.router, prefix="/api")
    app.include_router(domains.router, prefix="/api")
    app.include_router(jobs.jobs_router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(principals.router, prefix="/api")
    app.include_router(collections.router, prefix="/api")
    app.include_router(courses.router, prefix="/api")
    app.include_router(books.router, prefix="/api")
    app.include_router(catalog.router, prefix="/api")
    app.include_router(backups.router, prefix="/api")
    app.include_router(licenses.router, prefix="/api")
    app.include_router(integrations.router, prefix="/api")
    app.include_router(system.system_router, prefix="/api")
    app.router.routes.append(
        Route(
            "/mcp",
            McpEndpoint(
                StreamableHTTPASGIApp(mcp.session_manager),
                lambda: app.state.sessionmaker,
                RateLimiter(settings.mcp_client_rate_per_minute)
                if settings.mcp_client_rate_per_minute > 0
                else None,
            ),
        )
    )
    if web_dir_ready(settings.web_dir):  # por último: só apanha o que a API não reclamou
        app.mount("/", SpaStaticFiles(directory=settings.web_dir, html=True), name="web")
    return app
