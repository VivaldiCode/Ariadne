"""A aplicação web (React + Vite) servida como estáticos pelo próprio server (`web/dist`).

O `index.html` responde também aos caminhos da aplicação (`/ler`, `/grafo`, …) para o router do
cliente funcionar ao recarregar a página; a API (`/api`), o MCP (`/mcp`) e o `/health` não passam
por aqui. Sem a pasta (desenvolvimento do server, testes) a aplicação simplesmente não é montada.
"""

from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# Prefixos que nunca devolvem o `index.html`: um erro de API tem de ser um 404 a sério.
RESERVED = ("api", "mcp", "health", "docs", "redoc", "openapi.json")


def web_dir_ready(directory: Path) -> bool:
    return (directory / "index.html").is_file()


class SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        fallback = False
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            first = path.split("/", 1)[0]
            # Um ficheiro com extensão em falta (um .js, um .png) é 404; um caminho da app não.
            if first in RESERVED or "." in path.rsplit("/", 1)[-1]:
                raise
            response = await super().get_response("index.html", scope)
            fallback = True
        if fallback or response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"  # o index.html nunca fica em cache
        elif path.startswith("assets/") and response.status_code == 200:
            # Só ficheiros que existem: os nomes levam o hash do conteúdo, cache para sempre.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
