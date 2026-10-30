"""`ariadne bootstrap`: o arranque de uma instalação nova, sem passos à mão (docs/STANDALONE.md).

Faz o que antes se fazia uma vez à mão, com os segredos que o dono pôs no `.env` (nenhum vem
do código): o setup do Wiki.js, os locales `en`+`pt` com o *namespacing* e o tema; o token do
Moodle; o utilizador, a biblioteca e a chave do Kavita; o papel Postgres limitado do gpu-gateway;
e a primeira chave de administrador da app.
Os tokens que estes serviços geram vão para o cofre (`ARIADNE_MASTER_KEY`), que o server e os
workers releem sozinhos: não é preciso mexer no `.env` nem reiniciar nada.

Idempotente: corre em cada `docker compose up` e só faz o que falta (um token que já funciona fica,
o tema só se aplica a um Wiki.js sem CSS). Um serviço com o URL vazio está num perfil do compose
que não subiu, e salta-se. As migrações são do `server` (aplica-as ao arrancar).
"""

import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
import typer
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.books.kavita import KavitaError
from ariadne.books.kavita import bootstrap as kavita_bootstrap
from ariadne.cli_courses import request_token
from ariadne.config import Settings, get_settings
from ariadne.courses.backend import CourseBackendError
from ariadne.courses.moodle import MoodleBackend
from ariadne.db import models
from ariadne.db.session import make_engine
from ariadne.vault.admin import VaultAdmin
from ariadne.vault.config import IntegrationConfig
from ariadne.vault.crypto import VaultError
from ariadne.wiki.bootstrap import BootstrapError, Theme, configure_site
from ariadne.wiki.bootstrap import bootstrap as wiki_bootstrap
from ariadne.wiki.language import WIKIJS_LOCALES
from ariadne.wiki.wikijs import WikiJsBackend

ACTOR = "bootstrap"


class Skip(Exception):
    """O serviço não está nesta instalação (URL vazio: o perfil dele está desligado)."""


# Quanto se espera que o contentor de um serviço apareça (o Moodle espera pelo Jobe, que é lento).
RESOLVE_WAIT = 300.0


def _deploy_file(*parts: str) -> Path | None:
    """Um ficheiro de deploy/: na imagem em /app/deploy, num checkout ao lado de server/."""
    here = Path(__file__).resolve()
    for root in (here.parents[1], here.parents[2]):
        path = root.joinpath("deploy", *parts)
        if path.exists():
            return path
    return None


def _theme_dir() -> Path | None:
    """deploy/wikijs-theme (o tema do Wiki.js)."""
    css = _deploy_file("wikijs-theme", "theme.css")
    return css.parent if css is not None else None


def _reachable(url: str, wait: float = 0.0) -> str:
    """O URL sem a barra final. `Skip` se está vazio (serviço desligado); erro se o host não resolve
    ao fim de `wait` segundos (o contentor ainda não existe enquanto espera pelas dependências)."""
    if not url.strip():
        raise Skip("empty URL")
    host = urlparse(url).hostname or ""
    deadline = time.monotonic() + wait
    while True:
        try:
            socket.getaddrinfo(host, None)
            return url.rstrip("/")
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise BootstrapError(
                    f"{host} does not exist: is its profile switched off? Then leave the URL "
                    "empty in the .env (see .env.example)"
                ) from exc
        time.sleep(3)


class Bootstrap:
    def __init__(self, settings: Settings, sessions: sessionmaker[Session], wait: float) -> None:
        self.config = IntegrationConfig(settings, sessions, ttl=0)
        self.admin = VaultAdmin(self.config, settings)
        self.sessions = sessions
        self.wait = wait
        state = self.admin.state()
        self.vault_problem: str | None = None if state.enabled else state.problem or "disabled"

    @property
    def settings(self) -> Settings:
        return self.config.effective()

    def keep(self, key: str, value: str) -> str:
        """Guarda um valor gerado por um serviço. Sem cofre, mostra-o para ir para o `.env`."""
        if self.config.resolve(key).value == value:
            return "already in the vault"
        if self.vault_problem is None:
            try:
                self.admin.set_value(key, value, ACTOR, action="bootstrap")
                self.config.invalidate()
                return "saved to the vault"
            except VaultError as exc:
                self.vault_problem = str(exc)
        typer.echo(f"  no vault ({self.vault_problem}): put {key.upper()}={value} in the .env")
        return "not saved"

    # --- serviços ---

    def wikijs(self) -> str:
        s = self.settings
        url = _reachable(s.wikijs_url, min(self.wait, RESOLVE_WAIT))
        token = s.wikijs_token
        status = "existing token"
        if not token or WikiJsBackend(url, token).active_locales() is None:
            email, password = s.wikijs_admin_email, s.wikijs_admin_password
            if not email or not password:
                raise BootstrapError(
                    "WIKIJS_ADMIN_EMAIL and WIKIJS_ADMIN_PASSWORD are missing from the .env"
                )
            token = wiki_bootstrap(
                url, email, password, s.wikijs_public_url, wait_seconds=self.wait
            )
            status = f"new token, {self.keep('wikijs_token', token)}"
        # WIKIJS_LOCALES vazio é «pergunta ao Wiki.js» no importador; aqui, todos os locales que o
        # Wiki.js encaminha (docs/WIKI-IMPORT.md 5.1), o do site primeiro.
        if s.wikijs_locales:
            locales = [p.strip() for p in s.wikijs_locales.split(",") if p.strip()]
        else:
            locales = sorted(WIKIJS_LOCALES, key=lambda code: (code != "pt", code))
        folder = _theme_dir()
        theme = Theme.load(folder, s.kiwix_public_url) if folder is not None else None
        changed = configure_site(url, token, locales, theme)
        return f"{status}; {', '.join(changed) or 'locales and theme were already set'}"

    def moodle(self) -> str:
        s = self.settings
        url = _reachable(s.moodle_url, min(self.wait, RESOLVE_WAIT))
        if s.moodle_token and self._moodle_ok(url, s.moodle_token):
            return "existing token"
        password = os.environ.get("MOODLE_WS_PASSWORD", "")
        if not password:
            raise BootstrapError("MOODLE_WS_PASSWORD is missing from the .env")
        user = os.environ.get("MOODLE_WS_USER") or "ariadnews"
        token, last = request_token(url, user, password, self.wait)
        if not token:
            raise BootstrapError(f"Moodle did not give a token ({last}); check the moodle logs")
        if not self._moodle_ok(url, token):
            raise BootstrapError("the Moodle token fails local_ariadne_ping")
        return f"new token, {self.keep('moodle_token', token)}"

    @staticmethod
    def _moodle_ok(url: str, token: str) -> bool:
        backend = MoodleBackend(url, token, timeout=60)
        try:
            backend.check()
            return True
        except CourseBackendError:
            return False
        finally:
            backend.close()

    def kavita(self) -> str:
        s = self.settings
        url = _reachable(s.kavita_url, min(self.wait, RESOLVE_WAIT))
        password = os.environ.get("KAVITA_ADMIN_PASSWORD", "")
        if not password:
            raise BootstrapError("KAVITA_ADMIN_PASSWORD is missing from the .env")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                if httpx.get(f"{url}/api/health", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise BootstrapError(f"Kavita does not answer at {url}")
            time.sleep(3)
        result = kavita_bootstrap(
            url,
            os.environ.get("KAVITA_ADMIN_USERNAME") or "ariadne",
            password,
            os.environ.get("KAVITA_ADMIN_EMAIL") or "ariadne@example.invalid",
            folder=s.kavita_books_path,
        )
        key = self.keep("kavita_api_key", result.api_key)
        self.keep("kavita_library_id", str(result.library_id))
        return f"library #{result.library_id}, key {key}"

    def gateway_role(self, role: str = "ariadne_gateway") -> str:
        """O papel Postgres limitado do gpu-gateway: corre deploy/postgres/gpu-gateway.sql, que
        só deixa ler e atualizar as colunas do túnel em `batch_run`. A
        palavra-passe vem do `.env`; se o papel já existe, troca-a e repete os GRANT."""
        password = os.environ.get("GPU_GATEWAY_DB_PASSWORD", "")
        if not password:
            raise Skip("no GPU_GATEWAY_DB_PASSWORD")
        script = _deploy_file("postgres", "gpu-gateway.sql")
        if script is None:
            raise BootstrapError("deploy/postgres/gpu-gateway.sql is missing")
        body = "\n".join(
            line
            for line in script.read_text(encoding="utf-8").splitlines()
            if not line.startswith("--")
        )
        with self.sessions() as session, session.begin():
            exists = session.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
            )
            # As variáveis do psql (:password, :"dbname") à mão: CREATE ROLE não aceita parâmetros.
            quoted = session.scalar(text("SELECT quote_literal(CAST(:p AS text))"), {"p": password})
            database = session.scalar(text("SELECT quote_ident(current_database())"))
            sql = (
                body.replace("ariadne_gateway", f'"{role}"')
                .replace(":password", str(quoted))
                .replace(':"dbname"', str(database))
            )
            if exists:
                sql = sql.replace("CREATE ROLE", "ALTER ROLE", 1)
            for statement in filter(str.strip, sql.split(";")):
                session.execute(text(statement))
        return f"role {role} " + ("updated" if exists else "created")

    def admin_key(self) -> str:
        with self.sessions() as session, session.begin():
            exists = session.scalar(
                select(models.Principal.id).where(
                    models.Principal.kind == PrincipalKind.user.value,
                    models.Principal.role == Role.admin.value,
                    models.Principal.revoked_at.is_(None),
                )
            )
            if exists is not None:
                return "an admin already exists"
            _, key = create_principal(session, PrincipalKind.user, "admin", Role.admin)
        typer.echo(
            "  Web app admin key (shown only now; save it and delete this log with "
            f"`docker compose rm -f bootstrap`):\n  {key}"
        )
        return "admin 'admin' created"


def bootstrap_command(
    wait: Annotated[
        float, typer.Option(help="Seconds to wait for each service (Moodle installs itself).")
    ] = 1200.0,
    skip_admin: Annotated[
        bool, typer.Option("--skip-admin", help="Do not create the first admin key.")
    ] = False,
    only: Annotated[
        str,
        typer.Option(
            help="Only these steps, comma-separated (wikijs, moodle, kavita, gpu-gateway, "
            "admin): compose runs one bootstrap per profile, so a failure in one service does "
            "not delay the others."
        ),
    ] = "",
) -> None:
    """Set up a new installation (Wiki.js, Moodle, Kavita, admin). Idempotent."""
    settings = get_settings()
    engine = make_engine(settings.database_url)
    boot = Bootstrap(settings, sessionmaker(engine), wait)
    if boot.vault_problem:
        typer.echo(f"Warning: the vault is not in effect ({boot.vault_problem}).", err=True)
    steps: list[tuple[str, Callable[[], str]]] = [
        ("wikijs", boot.wikijs),
        ("moodle", boot.moodle),
        ("kavita", boot.kavita),
        ("gpu-gateway", boot.gateway_role),
    ]
    if not skip_admin:
        steps.append(("admin", boot.admin_key))
    if only:
        wanted = {part.strip() for part in only.split(",") if part.strip()}
        unknown = wanted - {name for name, _ in steps} - {"admin"}
        if unknown:
            raise typer.BadParameter(f"unknown steps: {', '.join(sorted(unknown))}")
        steps = [(name, step) for name, step in steps if name in wanted]
    failed = []
    for name, step in steps:
        try:
            typer.echo(f"{name}: {step()}")
        except Skip as exc:
            typer.echo(f"{name}: skipped ({exc})")
        except (BootstrapError, KavitaError, CourseBackendError, VaultError) as exc:
            typer.echo(f"{name}: ERROR: {exc}", err=True)
            failed.append(name)
    engine.dispose()
    if failed:
        raise typer.Exit(1)
    typer.echo("Bootstrap complete.")
