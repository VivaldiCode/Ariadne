"""Cabeçalhos de segurança de todas as respostas HTTP (a app web, a API, o MCP, o `/docs`)."""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# A app web é toda do próprio servidor: scripts e estilos `self` (os estilos inline do React
# precisam de 'unsafe-inline'), imagens `self` ou `data:`, e ninguém a pode pôr numa `<iframe>`.
APP_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "frame-ancestors 'none'"
)

# O Swagger (`/docs`) e o ReDoc (`/redoc`) do FastAPI carregam o JS e o CSS de um CDN e têm um
# script inline: só nestas duas rotas a política é mais larga.
DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "font-src 'self' https://fonts.gstatic.com; worker-src blob:; frame-ancestors 'none'"
)
DOCS_PATHS = frozenset({"/docs", "/redoc"})

STATIC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


class SecurityHeaders:
    """Middleware ASGI puro (sem `BaseHTTPMiddleware`, que estraga respostas em streaming como as
    do MCP). Não sobrescreve um cabeçalho que a rota já tenha posto."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = DOCS_CSP if scope.get("path") in DOCS_PATHS else APP_CSP

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in {**STATIC_HEADERS, "content-security-policy": csp}.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
