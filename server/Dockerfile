# Contexto de build = a raiz do repositório (docker-compose.yml): a imagem leva o server e a app web.

# Etapa 1: compila a app web (React + Vite). Só o `dist` segue para a imagem final; o Node não
# corre no X5550 nem existe em runtime.
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
# O cloudflared do túnel do lote de GPU: o `ariadne gpu-gateway` liga-o só enquanto há uma corrida com
# token (ariadne/compute/tunnel.py). Binário Go estático, sem requisitos de CPU além do x86-64 de base.
COPY --from=cloudflare/cloudflared:2026.9.3 /usr/local/bin/cloudflared /usr/local/bin/cloudflared

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# O git guarda os cursos canónicos (um commit por alteração; `ariadne.courses.repo`). O `age` cifra as
# releases (só conhece a chave pública do destinatário: ariadne.release) e o `pg_dump` 17 (do repositório
# PGDG: o do Debian é mais antigo do que o servidor) faz o backup local (ariadne.backup). Ambos são
# binários estáveis sem requisitos de CPU além do x86-64 de base. O `ssh-keygen` (openssh-client) assina
# o manifesto das releases (`ssh-keygen -Y sign`; ariadne.release.sign).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git age openssh-client ca-certificates curl \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependências primeiro (camada em cache enquanto o uv.lock não muda).
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra kaggle

COPY server/ariadne ./ariadne
COPY server/migrations ./migrations
COPY server/alembic.ini ./
# O notebook do lote em GPU externa (o worker envia-o ao Kaggle; ver docs/WIKI-IMPORT.md, secção 7).
COPY deploy/gpu ./deploy/gpu
# O tema do Wiki.js: o `ariadne bootstrap` aplica-o a uma instalação nova (docs/STANDALONE.md).
COPY deploy/wikijs-theme/theme.css deploy/wikijs-theme/head.html deploy/wikijs-theme/body.html \
     deploy/wikijs-theme/logo-url.txt ./deploy/wikijs-theme/
# O papel limitado do gpu-gateway: o `ariadne bootstrap` corre-o (o mesmo SQL do ambiente).
COPY deploy/postgres/gpu-gateway.sql ./deploy/postgres/
COPY --from=web /web/dist ./web
RUN uv sync --frozen --no-dev --extra kaggle

RUN useradd --uid 1000 --home-dir /app ariadne \
  && mkdir -p /models /backups /courses && chown ariadne:ariadne /models /backups /courses   # os volumes herdam o dono
USER ariadne

EXPOSE 8800
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8800/health', timeout=4)"]

CMD ["sh", "-c", "ariadne migrate && exec ariadne serve"]
