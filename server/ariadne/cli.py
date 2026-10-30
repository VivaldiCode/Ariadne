import logging
import math
import os
import signal
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
import uvicorn
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ariadne import router
from ariadne.auth import PrincipalKind, Role, Scope, create_principal, find_principal
from ariadne.cli_batch import batch_app
from ariadne.cli_books import books_app, kavita_app, licenses_app
from ariadne.cli_bootstrap import bootstrap_command
from ariadne.cli_courses import courses_app, moodle_app, sandbox_app
from ariadne.cli_release import backup_app, release_app
from ariadne.cli_secrets import secrets_app
from ariadne.concepts import importers as concept_importers
from ariadne.concepts import origin as concept_origin
from ariadne.config import Settings, get_settings
from ariadne.db import migrate as db_migrate
from ariadne.db.session import make_engine
from ariadne.documents import LibraryError, make_document_id, read_article
from ariadne.embedder import EmbedderError, get_embedder
from ariadne.licenses import LicenseBlocked, assert_exportable, assert_may_send, embedder_endpoint
from ariadne.mcp.server import build_mcp_server, stdio_caller
from ariadne.mempalace_store import MemPalaceError, MemPalaceStore, mempalace_dsn
from ariadne.rooms import list_rooms
from ariadne.rooms_build import run_rooms
from ariadne.semantic import PgVectorStore
from ariadne.services import Services
from ariadne.sources.base import NotAnArticle, NotFound
from ariadne.sources.registry import SourceRegistry
from ariadne.vault.config import effective_settings
from ariadne.vectors import (
    ParquetSink,
    VectorsError,
    collect_aliases,
    iter_article_vectors,
    missing_sources,
    read_parquet,
    store_vectors,
)
from ariadne.warm import not_yet_wanted, seed_path
from ariadne.wiki.bootstrap import BootstrapError
from ariadne.wiki.bootstrap import bootstrap as wiki_bootstrap
from ariadne.wiki.refresh import enqueue_import
from ariadne.workers.indexer import enqueue_index
from ariadne.workers.runner import Runner, UnknownWorker, parse_workers

app = typer.Typer(help="Ariadne: offline context memory.", no_args_is_help=True)
principal_app = typer.Typer(help="Users and agents that call the API/MCP.")
app.add_typer(principal_app, name="principal")
sources_app = typer.Typer(help="Knowledge sources (ZIM files in ZIM_DIR).")
app.add_typer(sources_app, name="sources")
index_app = typer.Typer(help="Level 3 index (indexing queue).")
app.add_typer(index_app, name="index")
wiki_app = typer.Typer(help="Level 2: Wiki.js (bootstrap and selective import).")
app.add_typer(wiki_app, name="wiki")
vectors_app = typer.Typer(
    help="Article vectors (title + first paragraph) for the cross-source graph."
)
app.add_typer(vectors_app, name="vectors")
rooms_app = typer.Typer(help="Rooms (MemPalace): topic groups of articles inside each wing.")
app.add_typer(rooms_app, name="rooms")
mempalace_app = typer.Typer(help="Mirror to MemPalace (optional extra ariadne[mempalace]).")
app.add_typer(mempalace_app, name="mempalace")
concepts_app = typer.Typer(
    help="Concepts and links between languages (sitelinks, langlinks, origin)."
)
app.add_typer(concepts_app, name="concepts")
app.add_typer(courses_app, name="courses")
app.add_typer(moodle_app, name="moodle")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(books_app, name="books")
app.add_typer(batch_app, name="batch")
app.add_typer(kavita_app, name="kavita")
app.add_typer(licenses_app, name="licenses")
app.add_typer(backup_app, name="backup")
app.add_typer(release_app, name="release")
app.add_typer(secrets_app, name="secrets")
app.command("bootstrap")(bootstrap_command)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on (empty = all).")] = "",
    port: Annotated[int, typer.Option(help="Port (API + MCP + web app).")] = 8800,
    reload: Annotated[bool, typer.Option(help="Reload when the code changes (dev).")] = False,
) -> None:
    """Start the HTTP server."""
    uvicorn.run("ariadne.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command("gpu-gateway")
def gpu_gateway(
    host: Annotated[str, typer.Option(help="Address to listen on (empty = all).")] = "",
    port: Annotated[
        int, typer.Option(help="Port (the one the Cloudflare tunnel points to).")
    ] = 8810,
) -> None:
    """Start the GPU batch gateway (the 4 `/gpu/v1/…` routes the tunnel exposes). Uses
    `GPU_GATEWAY_DATABASE_URL` (a restricted Postgres role, see deploy/postgres/gpu-gateway.sql)
    or, without it, `DATABASE_URL`. With `GPU_TUNNEL_CREDENTIALS_FILE` it also starts and stops
    `cloudflared` by itself: only while a run with a token is active (ariadne/compute/tunnel.py)."""
    import tempfile
    import threading

    from sqlalchemy.orm import sessionmaker

    from ariadne.compute.gateway import create_gateway_app
    from ariadne.compute.tunnel import supervisor_for
    from ariadne.db.session import make_engine

    settings = get_settings()
    engine = make_engine(settings.gpu_gateway_database_url or settings.database_url)
    sessions = sessionmaker(engine)
    stop = threading.Event()
    supervisor = None
    if settings.gpu_tunnel_credentials_file:
        if not settings.gpu_gateway_public_url:
            raise typer.BadParameter(
                "GPU_TUNNEL_CREDENTIALS_FILE is set but GPU_GATEWAY_PUBLIC_URL is not."
            )
        supervisor = supervisor_for(
            sessions,
            settings.gpu_gateway_public_url,
            settings.gpu_tunnel_credentials_file,
            port=port,
            workdir=Path(tempfile.mkdtemp(prefix="ariadne-tunel-")),
        )
        threading.Thread(target=supervisor.run, args=(stop,), name="tunel", daemon=True).start()
    try:
        uvicorn.run(create_gateway_app(sessions), host=host, port=port)
    finally:
        stop.set()
        if supervisor is not None:
            supervisor.stop()


@app.command()
def ping() -> None:
    """Connect to the database and exit 0 if it answers: the healthcheck for workers (which have
    no HTTP). A worker that dies takes its container with it; one left without a database goes
    `unhealthy`."""
    from sqlalchemy import text

    engine = make_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        typer.echo(f"Database unavailable: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()


@app.command()
def migrate(
    revision: Annotated[str, typer.Argument(help="Target Alembic revision.")] = "head",
) -> None:
    """Apply the database migrations."""
    db_migrate.upgrade(get_settings().database_url, revision)
    typer.echo(f"Database at {revision}.")


@app.command()
def worker(
    all_workers: Annotated[bool, typer.Option("--all", help="Run all workers.")] = False,
    only: Annotated[
        str | None,
        typer.Option(help="Only these, comma-separated (e.g. indexer,importer,curator,decay)."),
    ] = None,
) -> None:
    """Run the workers: indexer (`index_document` and `article_vector` queues), decay (compacts
    the heat), neighbors (expands the neighbourhood of hot documents), linker (links documents
    from different sources) and rooms (groups each wing's articles into rooms)."""
    try:
        names = parse_workers(only, all_workers)
    except UnknownWorker as exc:
        typer.echo(f"{exc} Use --all or --only.", err=True)
        raise typer.Exit(2) from exc
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    services = Services.open(get_settings())
    runner = Runner(services, names)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: runner.stop.set())  # acaba o job em curso e sai
    try:
        services.sync_sources()
        runner.run_forever()
    finally:
        services.close()


@app.command("mcp-stdio")
def mcp_stdio() -> None:
    """MCP server over stdio, for development. The principal comes from ARIADNE_API_KEY."""
    settings = get_settings()  # stdout é o canal do protocolo: tudo o que avisa vai para stderr
    if not settings.ariadne_api_key:
        typer.echo(
            "ARIADNE_API_KEY is missing (the key from `ariadne principal create`).", err=True
        )
        raise typer.Exit(1)
    services = Services.open(settings)
    try:
        with services.sessions() as session:
            principal = find_principal(session, settings.ariadne_api_key)
            principal_id = principal.id if principal is not None else None
        if principal_id is None:
            typer.echo("ARIADNE_API_KEY does not match any principal.", err=True)
            raise typer.Exit(1)
        services.sync_sources()
        caller = stdio_caller(
            services.sessions, settings.ariadne_api_key, f"stdio-{uuid4().hex[:12]}"
        )
        build_mcp_server(
            lambda: services, caller, max_translate_chars=settings.translate_max_chars
        ).run("stdio")
    finally:
        services.close()


@principal_app.command("create")
def principal_create(
    name: Annotated[str, typer.Option(help="Readable name (e.g. 'claude-desktop').")],
    kind: Annotated[PrincipalKind, typer.Option(help="Principal kind.")] = PrincipalKind.agent,
    role: Annotated[
        Role, typer.Option(help="Role: 'admin' manages principals and keys (in the web app).")
    ] = Role.member,
    scope: Annotated[
        Scope,
        typer.Option(help="Âmbito: 'mcp' para clientes externos (só MCP, só conteúdo aberto)."),
    ] = Scope.full,
) -> None:
    """Create a principal and show its API key (this one time only). The web app's first admin
    is created here: `--kind user --role admin`."""
    if role is Role.admin and kind is not PrincipalKind.user:
        typer.echo("Only a principal of kind 'user' can be an admin.", err=True)
        raise typer.Exit(2)
    if scope is Scope.mcp and (kind is not PrincipalKind.agent or role is not Role.member):
        typer.echo("An 'mcp' key can only belong to a 'member' agent.", err=True)
        raise typer.Exit(2)
    engine = make_engine(get_settings().database_url)
    with Session(engine) as session, session.begin():
        principal, key = create_principal(session, kind, name, role, scope)
        principal_id = principal.id
    engine.dispose()
    typer.echo(f"Principal {principal_id} ({kind.value}, {role.value}) '{name}' created.")
    typer.echo(f"API key (save it now, it will not be shown again):\n{key}")


@sources_app.command("sync")
def sources_sync() -> None:
    """Scan ZIM_DIR and update the `source` table (sources that vanished become 'missing')."""
    settings = get_settings()
    engine = make_engine(settings.database_url)
    registry = SourceRegistry.from_settings(settings)
    try:
        with Session(engine) as session:
            report = registry.sync(session)
    finally:
        registry.close()
        engine.dispose()
    for source_id in report.active:
        typer.echo(f"active   {source_id}")
    for source_id in report.missing:
        typer.echo(f"missing  {source_id}")
    for file, source_id in report.duplicates.items():
        typer.echo(f"duplicate {file} (the latest version of {source_id} is kept)", err=True)
    for file, reason in report.errors.items():
        typer.echo(f"error    {file}: {reason}", err=True)
    typer.echo(f"{len(report.active)} active sources in {settings.zim_dir}.")


@sources_app.command("classify")
def sources_classify() -> None:
    """How each source was classified on entry: article, course or book (STRUCTURE.md, 4)."""
    settings = get_settings()
    registry = SourceRegistry.from_settings(settings)
    try:
        report = registry.load()
        for source in registry.active():
            meta = source.meta
            archive = (
                " (archive: the content comes from the extracted course)"
                if meta.get("archive_only")
                else ""
            )
            typer.echo(
                f"{source.id}: {meta.get('content_class', 'article')}{archive}"
                f" — {meta.get('class_reason', '')}"
            )
        for file, reason in report.errors.items():
            typer.echo(f"error    {file}: {reason}", err=True)
    finally:
        registry.close()


SEED_BATCH = 500


@index_app.command("enqueue")
def index_enqueue(
    source: Annotated[str, typer.Option(help="Source id (see `ariadne sources sync`).")],
    paths: Annotated[
        Path | None,
        typer.Option(help="File with one path or title per line (cold start)."),
    ] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="At most this many documents.")] = None,
    priority: Annotated[
        float, typer.Option(help="Queue priority; real usage starts at 0.3.")
    ] = 0.1,
) -> None:
    """Seed level 3: queue the documents of a list (or the source's first `--limit`) for
    indexing. Indexing respects the daily budget; real usage jumps the queue."""
    if paths is None and limit is None:
        typer.echo("Give --paths (a list of paths/titles) or --limit.", err=True)
        raise typer.Exit(2)
    if not math.isfinite(priority) or priority < 0:
        typer.echo("The priority must be a finite number >= 0.", err=True)
        raise typer.Exit(2)
    settings = get_settings()
    engine = make_engine(settings.database_url)
    registry = SourceRegistry.from_settings(settings)
    store = PgVectorStore()
    queued = ignored = known = 0
    try:
        model_id = _current_model_id(settings)
        with Session(engine) as session:
            registry.sync(session)
            found = registry.get(source)
            if found is None:
                names = ", ".join(s.id for s in registry.active()) or "(none)"
                typer.echo(f"Unknown source: {source!r}. Available: {names}.", err=True)
                raise typer.Exit(1)
            seen: set[str] = set()
            batch: list[str] = []

            def flush() -> None:
                nonlocal queued, known
                room = None if limit is None else limit - queued
                todo = not_yet_wanted(session, store, model_id, batch)
                known += len(batch) - len(todo)
                for document_id in todo[:room]:
                    enqueue_index(session, document_id, priority)
                    queued += 1
                session.commit()
                batch.clear()

            for line in _seed_lines(paths) if paths is not None else found.iter_paths():
                if limit is not None and queued >= limit:
                    break
                if paths is not None:  # títulos e grafias à mão: resolve-se (e segue redirects)
                    try:
                        path = read_article(found, seed_path(line, found.id)).path
                    except (NotFound, NotAnArticle):
                        ignored += 1
                        typer.echo(f"ignored (not an article of this source): {line}", err=True)
                        continue
                else:  # `iter_paths` já dá caminhos canónicos de artigos: nada a ler
                    path = line
                document_id = make_document_id(found.id, path)
                if document_id in seen:
                    continue
                seen.add(document_id)
                batch.append(document_id)
                size = SEED_BATCH if limit is None else min(SEED_BATCH, limit - queued)
                if len(batch) >= size:
                    flush()
                    if limit is not None and queued >= limit:
                        break
            if batch and (limit is None or queued < limit):
                flush()
    finally:
        registry.close()
        engine.dispose()
    typer.echo(
        f"{queued} documents queued for {source} (priority {priority}); "
        f"{known} already indexed or recently failed; {ignored} ignored."
    )


def _current_model_id(settings: Settings) -> str | None:
    """O `model_id` dos vetores de hoje (não carrega o modelo), ou `None` sem embedder."""
    try:
        return get_embedder(settings).model_id
    except EmbedderError:
        return None


def _seed_lines(file: Path) -> Iterator[str]:
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


@vectors_app.command("build")
def vectors_build(
    source: Annotated[str, typer.Option(help="Source id (see `ariadne sources sync`).")],
    limit: Annotated[int | None, typer.Option(min=1, help="At most this many articles.")] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Write a Parquet file (to import on the server) instead of saving to the "
            "database. This mode needs no Postgres: it is meant to run on the Mac."
        ),
    ] = None,
    batch: Annotated[int, typer.Option(min=1, help="Articles vectorised at a time.")] = 64,
) -> None:
    """Generate the vector of each article in the source (title + first paragraph, articles
    only). With `--out` it writes Parquet (heavy load, on the Mac); without `--out` it saves
    straight to the database, skipping articles that already have a vector for the current
    model."""
    settings = (
        get_settings()
        if out is None
        else Settings(database_url=os.environ.get("DATABASE_URL", "postgresql+psycopg://sem/base"))
    )
    registry = SourceRegistry.from_settings(settings)
    engine = make_engine(settings.database_url) if out is None else None
    store = PgVectorStore()
    total = 0
    try:
        try:
            embedder = get_embedder(settings)
            model_id = embedder.model_id
        except EmbedderError as exc:
            typer.echo(f"Embedder unavailable: {exc}", err=True)
            raise typer.Exit(1) from exc
        if engine is None:
            _guard_export(settings, source)  # `--out` leva vetores para fora do servidor
            registry.load()
        else:
            with Session(engine) as session:
                registry.sync(session)
        found = registry.get(source)
        if out is not None and found is not None:
            try:
                assert_exportable(None, output="vectors build --out", sources=[found])
            except LicenseBlocked as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc
        if found is None:
            names = ", ".join(s.id for s in registry.active()) or "(none)"
            typer.echo(f"Unknown source: {source!r}. Available: {names}.", err=True)
            raise typer.Exit(1)
        if engine is not None:  # um embedder remoto não recebe texto privado
            try:
                with Session(engine) as guard:
                    assert_may_send(
                        guard,
                        target="embeddings",
                        url=embedder_endpoint(settings),
                        allowed=settings.private_allowed_hosts,
                        source_ids=[source],
                    )
            except LicenseBlocked as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc
        typer.echo(f"{source}: {found.article_count()} articles, model {model_id}.", err=True)
        aliases = collect_aliases(found)
        if out is not None:
            with ParquetSink(out) as sink:
                for rows in iter_article_vectors(
                    found, embedder, limit=limit, batch=batch, aliases=aliases
                ):
                    sink.write(rows)
                    total += len(rows)
                    typer.echo(f"  {total} vectors", err=True)
        else:
            assert engine is not None
            with Session(engine) as session:

                def skip(document_ids: list[str]) -> set[str]:
                    return store.has_article_vectors(session, document_ids, model_id)

                for rows in iter_article_vectors(
                    found, embedder, limit=limit, batch=batch, aliases=aliases, skip=skip
                ):
                    total += store_vectors(session, store, rows)
                    session.commit()
                    typer.echo(f"  {total} vectors saved", err=True)
    finally:
        registry.close()
        if engine is not None:
            engine.dispose()
    where = str(out) if out is not None else "the database"
    typer.echo(f"{total} vectors of {source} in {where}.")


def _guard_export(settings: Settings, source_id: str) -> None:
    """Uma saída para fora do NAS (`vectors build --out`) recusa fontes privadas. Se a base de
    dados responde, pergunta-lhe pela classe (os livros só lá estão); senão, a verificação feita
    com a fonte aberta dos ficheiros (a seguir) é a que vale."""
    if "DATABASE_URL" not in os.environ:
        return
    engine = make_engine(settings.database_url)
    try:
        with Session(engine) as session:
            try:
                assert_exportable(session, output="vectors build --out", source_ids=[source_id])
            except LicenseBlocked as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc
    except OperationalError:
        return  # sem base de dados (o Mac): vale a classe da fonte aberta dos ficheiros
    finally:
        engine.dispose()


@vectors_app.command("import")
def vectors_import(
    file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Parquet from `build`.")
    ],
) -> None:
    """Import article vectors from a Parquet file (idempotent: repeating it neither duplicates
    nor changes anything already identical). Refuses files from another model or dimension."""
    settings = get_settings()
    model_id = _current_model_id(settings)
    if model_id is None:
        typer.echo("Embedder unavailable: the current model is unknown.", err=True)
        raise typer.Exit(1)
    engine = make_engine(settings.database_url)
    store = PgVectorStore()
    seen = changed = 0
    try:
        with Session(engine) as session:
            known: set[str] = set()
            for rows in read_parquet(file, expected_model_id=model_id):
                if absent := missing_sources(session, sorted({r.source_id for r in rows} - known)):
                    typer.echo(
                        f"Sources not registered: {', '.join(absent)}. Run `ariadne sources "
                        "sync` with the ZIMs in ZIM_DIR and try again.",
                        err=True,
                    )
                    raise typer.Exit(1)
                known |= {r.source_id for r in rows}
                changed += store_vectors(session, store, rows)
                session.commit()
                seen += len(rows)
                typer.echo(f"  {seen} vectors read", err=True)
    except VectorsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()
    typer.echo(f"{seen} vectors read from {file}; {changed} saved or changed ({model_id}).")


# --- salas (Fase 9) -----------------------------------------------------------------------------


@rooms_app.command("build")
def rooms_build(
    wing: Annotated[str | None, typer.Option(help="Only this wing (default: all).")] = None,
    force: Annotated[
        bool, typer.Option("--force/--if-needed", help="Recompute even if the wing has not grown.")
    ] = True,
) -> None:
    """Compute each wing's rooms from the article vectors (`ariadne vectors build`) and assign
    the new documents. With `--if-needed` it only recomputes the wings that have grown."""
    settings = get_settings()
    services = Services.open(settings)
    try:
        if router.current_model_id(services) is None:
            typer.echo("Embedder unavailable: the current model is unknown.", err=True)
            raise typer.Exit(1)
        run = run_rooms(services, wing=wing, force=force)
    finally:
        services.close()
    if not run.wings:
        typer.echo("No wing has article vectors (run `ariadne vectors build`).")
    for report in run.wings:
        if report.skipped:
            typer.echo(f"{report.wing}: {report.skipped}.")
        elif report.rebuilt:
            typer.echo(
                f"{report.wing}: {report.documents} articles in {report.rooms} rooms "
                f"({report.created} new, {report.kept} kept, {report.removed} removed)."
            )
        else:
            typer.echo(
                f"{report.wing}: {report.rooms} rooms up to date, "
                f"{report.assigned} articles assigned."
            )


@rooms_app.command("list")
def rooms_list(
    wing: Annotated[str | None, typer.Option(help="Only this wing.")] = None,
    limit: Annotated[int, typer.Option(min=1, help="At most this many rooms.")] = 50,
) -> None:
    """List the rooms (largest first)."""
    engine = make_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            found = list_rooms(session, wing=wing, limit=limit)
    finally:
        engine.dispose()
    for room in found:
        typer.echo(f"{room.id:>6}  {room.size:>7}  {room.wing}  {room.label}")
    typer.echo(f"{len(found)} rooms.")


# --- MemPalace (Fase 9) -------------------------------------------------------------------------


@mempalace_app.command("sync")
def mempalace_sync(
    wing: Annotated[str | None, typer.Option(help="Only this wing (default: all).")] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Resend everything, even what was already mirrored.")
    ] = False,
) -> None:
    """Mirror the indexed chunks to MemPalace, with each one's wing and room (one way:
    Ariadne → MemPalace; Ariadne's tables remain the source of truth). Needs the
    `ariadne[mempalace]` extra; incremental unless the rooms changed or `--full` is given."""
    settings = effective_settings()
    model_id = _current_model_id(settings)
    if model_id is None:
        typer.echo("Embedder unavailable: the current model is unknown.", err=True)
        raise typer.Exit(1)
    engine = make_engine(settings.database_url)
    try:
        store = MemPalaceStore(mempalace_dsn(settings), settings.mempalace_palace_dir)
        with Session(engine) as session:
            report = store.sync(session, model_id, wing=wing, full=full)
    except MemPalaceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()
    for name, count in report.wings.items():
        kind = "full" if report.full[name] else "incremental"
        typer.echo(f"{name}: {count} drawers ({kind}).")
    typer.echo(
        f"{report.drawers} drawers saved, {report.removed} orphans deleted "
        f"({settings.mempalace_palace_dir})."
    )


# --- Wiki.js ---------------------------------------------------------------------------------


def _read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE do `.env` (sem aspas nem expansões): só para o `wiki bootstrap`."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def _set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


@wiki_app.command("bootstrap")
def wiki_bootstrap_command(
    url: Annotated[
        str | None,
        typer.Option(help="Wiki.js URL (default WIKIJS_URL, else http://localhost:8802)."),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option(
            help="The .env: missing WIKIJS_ADMIN_* values are read from it and the new "
            "WIKIJS_TOKEN is written to it (instead of being printed)."
        ),
    ] = None,
    wait: Annotated[float, typer.Option(help="Seconds to wait for Wiki.js.")] = 90.0,
) -> None:
    """Prepare Wiki.js: setup if it is new, API enabled and a new API key (WIKIJS_TOKEN).
    Needs WIKIJS_ADMIN_EMAIL and WIKIJS_ADMIN_PASSWORD (from the environment or --env-file)."""
    from_file = _read_env_file(env_file) if env_file is not None and env_file.exists() else {}

    def setting(name: str) -> str:
        return os.environ.get(name) or from_file.get(name, "")

    base = url or setting("WIKIJS_URL") or f"http://localhost:{setting('WIKIJS_PORT') or 8802}"
    public = setting("WIKIJS_PUBLIC_URL") or f"http://localhost:{setting('WIKIJS_PORT') or 8802}"
    try:
        key = wiki_bootstrap(
            base,
            setting("WIKIJS_ADMIN_EMAIL"),
            setting("WIKIJS_ADMIN_PASSWORD"),
            public,
            wait_seconds=wait,
        )
    except BootstrapError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if env_file is not None:
        _set_env_value(env_file, "WIKIJS_TOKEN", key)
        typer.echo(f"API key created and saved to WIKIJS_TOKEN ({env_file}).")
        typer.echo("Restart the server and worker to use it: docker compose up -d")
    else:
        typer.echo("Wiki.js API key (save it as WIKIJS_TOKEN in the .env):")
        typer.echo(key)


@wiki_app.command("import")
def wiki_import(
    source: Annotated[str, typer.Option(help="Source id (see `ariadne sources sync`).")],
    paths: Annotated[
        Path | None,
        typer.Option(help="File with one path or title per line (a hand-picked collection)."),
    ] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="At most this many documents.")] = None,
    priority: Annotated[float, typer.Option(help="Queue priority.")] = 0.2,
    force: Annotated[bool, typer.Option(help="Resend the page even if it is identical.")] = False,
) -> None:
    """Queue the documents of a list (or the source's first `--limit`) for import into Wiki.js.
    It never imports a whole source: without --paths or --limit it refuses. Whatever needs
    curation goes through the pipeline (flags, curator) before being published."""
    if paths is None and limit is None:
        typer.echo("Give --paths (a list of paths/titles) or --limit.", err=True)
        raise typer.Exit(2)
    if not math.isfinite(priority) or priority < 0:
        typer.echo("The priority must be a finite number >= 0.", err=True)
        raise typer.Exit(2)
    settings = get_settings()
    engine = make_engine(settings.database_url)
    registry = SourceRegistry.from_settings(settings)
    queued = ignored = 0
    try:
        with Session(engine) as session:
            registry.sync(session)
            found = registry.get(source)
            if found is None:
                names = ", ".join(s.id for s in registry.active()) or "(none)"
                typer.echo(f"Unknown source: {source!r}. Available: {names}.", err=True)
                raise typer.Exit(1)
            seen: set[str] = set()
            for line in _seed_lines(paths) if paths is not None else found.iter_paths():
                if limit is not None and queued >= limit:
                    break
                if paths is not None:
                    try:
                        path = read_article(found, seed_path(line, found.id)).path
                    except (NotFound, NotAnArticle):
                        ignored += 1
                        typer.echo(f"ignored (not an article of this source): {line}", err=True)
                        continue
                else:
                    path = line
                document_id = make_document_id(found.id, path)
                if document_id in seen:
                    continue
                seen.add(document_id)
                enqueue_import(session, document_id, priority, force=force)
                queued += 1
            session.commit()
    finally:
        registry.close()
        engine.dispose()
    typer.echo(f"{queued} documents of {source} queued for import; {ignored} ignored.")


# --- conceitos e línguas (Fase 7) ----------------------------------------------------------------


def _library(settings: Settings) -> tuple[SourceRegistry, sessionmaker[Session]]:
    """As fontes ativas (a pasta de ZIM sincronizada com a base) e uma fábrica de sessões."""
    engine = make_engine(settings.database_url)
    registry = SourceRegistry.from_settings(settings)
    sessions = sessionmaker(engine)
    with sessions() as session:
        registry.sync(session)
    return registry, sessions


def _close_library(registry: SourceRegistry, sessions: sessionmaker[Session]) -> None:
    registry.close()
    if sessions.kw.get("bind") is not None:
        sessions.kw["bind"].dispose()


def _check_file(path: Path) -> None:
    if not path.is_file():
        typer.echo(f"File not found: {path}", err=True)
        raise typer.Exit(2)


def _show(report: concept_importers.ImportReport) -> None:
    """Imprime as contagens; um dump cortado importou-se até onde foi lido, mas sai com código 3
    para quem corre isto num script saber que a importação ficou incompleta."""
    for line in report.lines():
        typer.echo(line)
    if report.truncated:
        typer.echo(
            "Partial import: the file is truncated (incomplete download?). "
            "Download it again and repeat: the import is idempotent.",
            err=True,
        )
        raise typer.Exit(3)


@concepts_app.command("import-sitelinks")
def concepts_import_sitelinks(
    file: Annotated[
        Path,
        typer.Argument(
            help="wikidatawiki-latest-wb_items_per_site.sql.gz (or the uncompressed .sql)."
        ),
    ],
    sites: Annotated[
        str | None,
        typer.Option(help="Only these Wikidata sites, comma-separated (e.g. ptwiki,enwiki)."),
    ] = None,
    min_members: Annotated[
        int, typer.Option(min=1, help="Only create concepts with at least this many versions.")
    ] = 2,
) -> None:
    """Import the Wikidata *sitelinks*: create the concepts (QID), link them to the documents of
    the sources present (those that exist in the ZIMs) and create the `langlink` and `wikidata`
    edges. Reads the dump in a stream, in batches; it is idempotent."""
    _check_file(file)
    chosen = {s.strip() for s in sites.split(",") if s.strip()} if sites else None
    registry, sessions = _library(get_settings())
    try:
        report = concept_importers.import_sitelinks(
            sessions,
            registry.active(),
            file,
            sites=chosen,
            min_members=min_members,
            progress=typer.echo,
        )
    except concept_importers.ConceptImportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        _close_library(registry, sessions)
    _show(report)


@concepts_app.command("import-langlinks")
def concepts_import_langlinks(
    page: Annotated[Path, typer.Argument(help="<wiki>-latest-page.sql.gz of the Wikipedia.")],
    langlinks: Annotated[Path, typer.Argument(help="<wiki>-latest-langlinks.sql.gz.")],
    site: Annotated[str, typer.Option(help="The site of that Wikipedia (e.g. ptwiki).")],
) -> None:
    """Alternative without the Wikidata dump: link the articles of one Wikipedia to their
    versions in the other languages the library has, using the `page` and `langlinks` dumps
    (concepts without a QID)."""
    _check_file(page)
    _check_file(langlinks)
    registry, sessions = _library(get_settings())
    try:
        report = concept_importers.import_langlinks(
            sessions, registry.active(), page, langlinks, site=site, progress=typer.echo
        )
    except concept_importers.ConceptImportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        _close_library(registry, sessions)
    _show(report)


@concepts_app.command("set-origin")
def concepts_set_origin(
    concept: Annotated[str, typer.Argument(help="The QID (Q1187566) or the concept id.")],
    lang: Annotated[str | None, typer.Argument(help="Original language (e.g. pt).")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Clear the original language.")] = False,
) -> None:
    """Set a concept's original language by hand: the version in that language is the original."""
    if (lang is None) == (not clear):
        typer.echo("Give the language (e.g. pt) or --clear.", err=True)
        raise typer.Exit(2)
    engine = make_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            try:
                result = concept_origin.set_origin(session, concept, None if clear else lang)
            except LibraryError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc
            session.commit()
    finally:
        engine.dispose()
    label = result.wikidata_qid or f"#{result.id}"
    typer.echo(
        f"{label} ({result.label or '-'}): original language = {result.origin_lang or '(none)'}; "
        f"{result.documents} documents."
    )


@concepts_app.command("import-origins")
def concepts_import_origins(
    file: Annotated[Path, typer.Argument(help="TSV: QID<TAB>country code (ISO 3166-1 alpha-2).")],
) -> None:
    """Set the concepts' original language from a `QID<TAB>country` TSV (extracted offline from
    Wikidata, P17/P495). Only touches concepts that already exist; never replaces what was set
    by hand; multilingual countries or ones missing from the table leave the concept without
    an origin."""
    _check_file(file)
    engine = make_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            report = concept_origin.import_origins(session, file)
            session.commit()
    finally:
        engine.dispose()
    for line in report.summary():
        typer.echo(line)
