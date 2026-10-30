"""Comandos `ariadne books`, `ariadne kavita` e `ariadne licenses` (Fase 11)."""

import os
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer
from sqlalchemy import func, select

from ariadne import licenses
from ariadne.books import index as book_index
from ariadne.books.add import AddError, add_book
from ariadne.books.backend import LibraryError
from ariadne.books.kavita import KavitaError, bootstrap, sync_locations
from ariadne.books.zim_import import (
    ZimImportAborted,
    ZimImportError,
    ZimImportReport,
    import_zim_books,
)
from ariadne.db import models
from ariadne.services import Services
from ariadne.sources.license import LicenseClass, parse_license_class
from ariadne.vault.config import effective_settings

books_app = typer.Typer(help="Books: library (Calibre), extracted chapters and reader (Kavita).")
kavita_app = typer.Typer(help="Kavita: the web reader for the books.")
licenses_app = typer.Typer(help="License classes (open | private) and output blocks.")


def _services() -> Services:
    return Services.open(effective_settings())


def _class_option(value: str) -> LicenseClass:
    try:
        return parse_license_class(value)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


# --- books ----------------------------------------------------------------------------------------


@books_app.command("add")
def books_add(
    file: Annotated[Path, typer.Argument(help="EPUB, PDF or MOBI (MOBI is converted to EPUB).")],
    license_class: Annotated[
        str,
        typer.Option(
            "--license-class",
            help='REQUIRED: "open" (public domain, CC) or "private" (purchased, own). '
            "Private never leaves the NAS.",
        ),
    ],
    license_name: Annotated[
        str,
        typer.Option(
            "--license-name",
            help='REQUIRED: the license in words (e.g. "Public domain", "CC BY-NC 3.0", '
            '"Bought from Manning").',
        ),
    ],
    attribution: Annotated[str, typer.Option(help="Attribution text (author, source URL).")] = "",
    book_id: Annotated[
        str | None, typer.Option("--id", help="The book id (by default, derived from the title).")
    ] = None,
    title: Annotated[str | None, typer.Option(help="Title, if the metadata has none.")] = None,
    author: Annotated[
        list[str] | None,
        typer.Option(help="Author, if the metadata has none (repeat for several)."),
    ] = None,
) -> None:
    """Add a book: refuse DRM, store it in Calibre, extract the chapters and index them."""
    chosen = _class_option(license_class)
    services = _services()
    try:
        result = add_book(
            services,
            file,
            license_class=chosen,
            license_name=license_name,
            attribution=attribution,
            book_id=book_id,
            title=title,
            authors=author or (),
        )
    except (AddError, LibraryError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        services.close()
    state = "added" if result.created else "was already in the library"
    typer.echo(
        f"{result.book_id}: {result.title!r} {state} ({result.format.upper()}, "
        f"{result.chapters} chapters, class {result.license_class}). {result.note}"
    )
    if result.held:
        typer.echo(f"  {result.held} chapters flagged by curation.")
    typer.echo(
        f"  Searchable as book:{result.book_id}; `ariadne books index {result.book_id}` "
        "also requests the vectors (level 3)."
    )


@books_app.command("import-zim")
def books_import_zim(
    source: Annotated[
        str, typer.Option("--source", help="The ZIM source, e.g. zim:gutenberg_pt_all.")
    ],
    limit: Annotated[
        int | None,
        typer.Option(min=1, help="At most N books to process in this run (repeat for the rest)."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only says what it would do: writes nothing.")
    ] = False,
    license_name: Annotated[
        str | None,
        typer.Option(
            "--license-name",
            help='The license in words, if not the one the ZIM suggests (Gutenberg: "Public '
            'domain (Project Gutenberg)").',
        ),
    ] = None,
    retry_refused: Annotated[
        bool,
        typer.Option(
            "--retry-refused",
            help="Re-evaluate the books the language rule refused (for example after "
            "removing the book that occupied the work).",
        ),
    ] = False,
    scan: Annotated[
        bool, typer.Option("--scan/--no-scan", help="At the end, ask Kavita to scan the library.")
    ] = True,
) -> None:
    """Bring the books (EPUB, PDF, MOBI) of a ZIM into Calibre, the index and Kavita.

    In a book ZIM (gutenberg, wikibooks…) it walks the ZIM; in an article ZIM, it processes the
    documents the importer marked as books. The same book in several formats is imported only
    once; repeating the command duplicates nothing.

    Originals in every language and translations into languages where the work does not yet
    exist are imported; the same work is never repeated in the same language. What the rule
    refuses is counted in the report.
    """
    services = _services()
    try:
        aborted: str | None = None
        try:
            report = import_zim_books(
                services,
                source,
                limit=limit,
                dry_run=dry_run,
                license_name=license_name,
                retry_refused=retry_refused,
                progress=typer.echo,
            )
        except ZimImportAborted as exc:  # parou a meio: o que entrou fica, e diz-se até onde
            report, aborted = exc.report, str(exc)
        except ZimImportError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        except Exception as exc:  # a base de dados em baixo, o disco cheio…: sem traceback
            typer.echo(
                f"Error ({type(exc).__name__}): {' '.join(str(exc).split())[:300]}", err=True
            )
            raise typer.Exit(1) from exc
        _print_zim_report(report)
        if report.added and not dry_run:
            if scan:
                ok, message = _kavita_scan(services)
                typer.echo(
                    ("Kavita: " if ok else "Kavita (still to do: `ariadne kavita scan`): ")
                    + message
                )
            else:
                typer.echo("Still to do: ask Kavita to scan the library: `ariadne kavita scan`.")
        if aborted is not None:
            typer.echo(f"STOPPED: {aborted}", err=True)
            raise typer.Exit(1)
        if report.failures:
            typer.echo(f"{len(report.failures)} books failed (see above).", err=True)
            raise typer.Exit(2)
    finally:
        services.close()


MAX_LISTED = 30  # as recusadas podem ser milhares: lista-se o princípio


def _print_zim_report(report: ZimImportReport) -> None:
    heading = "Dry run" if report.dry_run else "Import"
    typer.echo(
        f"{heading} of {report.source_id}: {report.files} book files, "
        f"{report.books} distinct books."
    )
    if report.dry_run:
        typer.echo(f"  to add (or already existing): {len(report.would_add)}")
    else:
        typer.echo(f"  added: {len(report.added)}")
        typer.echo(f"  already existing: {len(report.existing)}")
    typer.echo(f"  duplicates ignored (other format): {len(report.duplicates)}")
    typer.echo(f"  already processed before: {report.skipped}")
    typer.echo(f"  refused by the language rule: {len(report.refused)}")
    if report.refused_before:
        typer.echo(
            f"  refused before: {report.refused_before} (`--retry-refused` re-evaluates them)"
        )
    for refused in report.refused[:MAX_LISTED]:
        typer.echo(f"    {refused.path} ({refused.title!r}): {refused.reason}")
    if len(report.refused) > MAX_LISTED:
        typer.echo(f"    … and {len(report.refused) - MAX_LISTED} more")
    if report.superseded:
        typer.echo(f"  superseded by an original: {len(report.superseded)}")
        for old in report.superseded[:MAX_LISTED]:
            typer.echo(
                f"    {old.book_id} ({old.language}) is now redundant; the original is {old.by}: "
                f"`ariadne books remove {old.book_id}` if you do not want it"
            )
    if report.pending:
        typer.echo(f"  left to process (limit): {report.pending}")
    typer.echo(f"  failures: {len(report.failures)}")
    for failure in report.failures:
        typer.echo(f"    {failure.path} ({failure.title!r}): {failure.reason}")
    for warning in report.warnings:
        typer.echo(f"  warning: {warning}")


@books_app.command("list")
def books_list() -> None:
    """The books in the library."""
    services = _services()
    try:
        with services.sessions() as session:
            rows = session.scalars(select(models.Book).order_by(models.Book.id)).all()
            for book in rows:
                where = "in Kavita" if book.kavita_series_id else "not in Kavita"
                typer.echo(
                    f"{book.id}  {book.title!r}  {book.format.upper()}  {book.chapters} ch.  "
                    f"{book.license_class}  {where}  [{book.status}]"
                )
            if not rows:
                typer.echo("No books. Add one with `ariadne books add`.")
    finally:
        services.close()


@books_app.command("show")
def books_show(book_id: str) -> None:
    """One book: metadata, license and chapters."""
    services = _services()
    try:
        with services.sessions() as session:
            book = session.get(models.Book, book_id)
            if book is None:
                typer.echo(f"Unknown book: {book_id!r}.", err=True)
                raise typer.Exit(1)
            typer.echo(f"{book.title} — {', '.join(book.authors) or 'unknown author'}")
            typer.echo(
                f"  {book.format.upper()}, {book.language or '?'}, {book.characters} characters, "
                f"calibre #{book.calibre_id}, {book.file_path}"
            )
            typer.echo(
                f"  license: {book.license_name} (class {book.license_class}); "
                f"extraction: {book.extraction_note}"
            )
            for chapter in session.scalars(
                select(models.BookChapter)
                .where(models.BookChapter.book_id == book_id)
                .order_by(models.BookChapter.position)
            ):
                held = f"  [{chapter.held_reason}]" if chapter.held_reason else ""
                pages = f" p.{chapter.start_page}-{chapter.end_page}" if chapter.start_page else ""
                typer.echo(
                    f"  {chapter.position:>3}. {chapter.title}{pages} ({chapter.characters}){held}"
                )
    finally:
        services.close()


@books_app.command("sync")
def books_sync(
    force: Annotated[bool, typer.Option(help="Extract again even what is already done.")] = False,
    index: Annotated[
        bool, typer.Option("--index", help="Also request the vectors of the chapters that changed.")
    ] = False,
) -> None:
    """Extract and index the missing books and locate them in Kavita."""
    services = _services()
    try:
        report = book_index.sync_books(services, force=force, enqueue=index)
        kavita = services.get_kavita()
        located = ""
        if kavita is not None and services.settings.kavita_library_id:
            with services.sessions() as session:
                found = sync_locations(
                    session,
                    kavita,
                    services.settings.kavita_library_id,
                    services.settings.kavita_books_path,
                )
            located = f"; Kavita: {found.mapped} located, {found.missing} not yet scanned"
    except KavitaError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        services.close()
    typer.echo(
        f"{report.books} books extracted, {report.chapters} chapters "
        f"({report.changed_chapters} changed, {report.held} flagged), "
        f"{report.enqueued} indexing requests{located}."
    )
    for error in report.errors:
        typer.echo(f"error: {error}", err=True)
    if report.errors:
        raise typer.Exit(1)


@books_app.command("index")
def books_index(
    book_id: str,
    priority: Annotated[float, typer.Option(help="Priority in the indexing queue.")] = 0.5,
) -> None:
    """Request vector indexing (level 3) for all the chapters of a book."""
    services = _services()
    try:
        queued = book_index.enqueue_book_index(services, book_id, priority)
    finally:
        services.close()
    typer.echo(f"{queued} chapters in the indexing queue." if queued else "Book has no chapters.")


@books_app.command("set-license")
def books_set_license(
    book_id: str,
    license_class: Annotated[str, typer.Argument(help="open or private")],
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirms you have the rights to make the book open.")
    ] = False,
) -> None:
    """Change the license class of a book (and of its chapters, chunks and links)."""
    chosen = _class_option(license_class)
    if chosen is LicenseClass.OPEN and not yes:
        typer.echo(
            'Making a book "open" asserts that you may redistribute it (it goes into the public '
            "releases). Repeat with --yes if you have that right.",
            err=True,
        )
        raise typer.Exit(2)
    services = _services()
    try:
        with services.sessions() as session:
            book = session.get(models.Book, book_id)
            if book is None:
                typer.echo(f"Unknown book: {book_id!r}.", err=True)
                raise typer.Exit(1)
            try:
                changed = licenses.set_source_class(session, f"book:{book_id}", chosen)
            except licenses.OverrideKeepsPrivate as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc
            session.commit()
            services.registry.sync(session)  # a fonte aberta em memória fica com a classe nova
    finally:
        services.close()
    typer.echo(f"{book_id}: class {chosen.value}; {changed} documents changed.")


@books_app.command("remove")
def books_remove(
    book_id: str,
    calibre: Annotated[
        bool, typer.Option("--calibre", help="Also delete the book (and its files) in Calibre.")
    ] = False,
) -> None:
    """Take the book out of search (it becomes `removed`; the documents are not deleted)."""
    services = _services()
    try:
        with services.sessions() as session:
            book = session.get(models.Book, book_id)
            if book is None:
                typer.echo(f"Unknown book: {book_id!r}.", err=True)
                raise typer.Exit(1)
            book.status = "removed"
            calibre_id = book.calibre_id
            session.commit()
            services.registry.sync(session)
        if calibre and calibre_id is not None:
            services.get_library().remove(calibre_id)
    except LibraryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        services.close()
    typer.echo(f"{book_id}: removed from the Ariadne library.")


@books_app.command("check")
def books_check() -> None:
    """Check Calibre, Kavita and the library folder."""
    settings = effective_settings()
    services = _services()
    failed = False
    try:
        for name in ("calibre", "kavita"):
            try:
                if name == "calibre":
                    info = services.get_library().check()
                else:
                    kavita = services.get_kavita()
                    if kavita is None:
                        raise KavitaError(
                            "Kavita is not configured (KAVITA_URL and KAVITA_API_KEY)."
                        )
                    info = kavita.check()
                typer.echo(f"{name}: ok {info}")
            except (LibraryError, KavitaError) as exc:
                failed = True
                typer.echo(f"{name}: {exc}", err=True)
        typer.echo(
            f"BOOKS_DIR {settings.books_dir}: "
            + ("exists" if settings.books_dir.is_dir() else "does NOT exist")
        )
    finally:
        services.close()
    if failed:
        raise typer.Exit(1)


# --- kavita ---------------------------------------------------------------------------------------


@kavita_app.command("bootstrap")
def kavita_bootstrap(
    url: Annotated[
        str | None,
        typer.Option(help="Kavita URL (default: KAVITA_URL or http://localhost:8806)."),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option(
            help="The .env: KAVITA_ADMIN_USERNAME and KAVITA_ADMIN_PASSWORD are read from it and "
            "KAVITA_API_KEY and KAVITA_LIBRARY_ID are written to it."
        ),
    ] = None,
    library: Annotated[str, typer.Option(help="Library name in Kavita.")] = "Ariadne",
    wait: Annotated[float, typer.Option(help="Seconds to wait for Kavita.")] = 120.0,
) -> None:
    """Connect Ariadne to Kavita: create the user (first time), the book library over the Calibre
    folder and start the scan; write the API key to KAVITA_API_KEY."""
    from ariadne.cli_courses import _env_values

    from_file = _env_values(env_file)

    def setting(name: str) -> str:
        return os.environ.get(name) or from_file.get(name, "")

    base = (
        url or setting("KAVITA_URL") or f"http://localhost:{setting('KAVITA_PORT') or 8806}"
    ).rstrip("/")
    username = setting("KAVITA_ADMIN_USERNAME") or "ariadne"
    password = setting("KAVITA_ADMIN_PASSWORD")
    if not password:
        typer.echo(
            "KAVITA_ADMIN_PASSWORD is missing (from the environment or --env-file).", err=True
        )
        raise typer.Exit(2)
    email = setting("KAVITA_ADMIN_EMAIL") or "ariadne@example.invalid"
    deadline = time.monotonic() + wait
    while True:
        try:
            if httpx.get(f"{base}/api/health", timeout=5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            typer.echo(
                f"Kavita at {base} is not responding. Start it: docker compose up -d kavita",
                err=True,
            )
            raise typer.Exit(1)
        time.sleep(3)
    try:
        result = bootstrap(
            base,
            username,
            password,
            email,
            library_name=library,
            folder=setting("KAVITA_BOOKS_PATH") or "/books",
        )
    except KavitaError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if env_file is not None:
        from ariadne.cli import _set_env_value

        _set_env_value(env_file, "KAVITA_API_KEY", result.api_key)
        _set_env_value(env_file, "KAVITA_LIBRARY_ID", str(result.library_id))
        typer.echo(f"Key written to KAVITA_API_KEY and KAVITA_LIBRARY_ID ({env_file}).")
        typer.echo("Restart the server and the worker to use them: docker compose up -d")
    else:
        typer.echo(
            f"Save in .env: KAVITA_API_KEY=<the key> and KAVITA_LIBRARY_ID={result.library_id}"
        )
        typer.echo(result.api_key)
    typer.echo(
        f"Kavita: user {'created' if result.created_user else 'existing'}, library "
        f"{library!r} #{result.library_id} {'created' if result.created_library else 'existing'}."
    )


@kavita_app.command("status")
def kavita_status() -> None:
    """Does Kavita respond? How many books have a place in the reader?"""
    services = _services()
    try:
        client = services.get_kavita()
        if client is None:
            typer.echo("Kavita is not configured (KAVITA_URL and KAVITA_API_KEY).", err=True)
            raise typer.Exit(1)
        try:
            typer.echo(f"kavita: ok {client.check()}")
        except KavitaError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        with services.sessions() as session:
            total = session.scalar(
                select(func.count()).select_from(models.Book).where(models.Book.status == "active")
            )
            placed = session.scalar(
                select(func.count())
                .select_from(models.Book)
                .where(models.Book.status == "active", models.Book.kavita_series_id.is_not(None))
            )
        typer.echo(f"{placed} of {total} books located in the reader.")
    finally:
        services.close()


def _kavita_scan(services: Services) -> tuple[bool, str]:
    """Pede ao Kavita que leia a biblioteca e localiza os livros lidos: (ok, mensagem)."""
    client = services.get_kavita()
    library_id = services.settings.kavita_library_id
    if client is None or not library_id:
        return False, "KAVITA_URL, KAVITA_API_KEY or KAVITA_LIBRARY_ID is missing."
    try:
        client.scan(library_id, force=True)
        time.sleep(5)
        with services.sessions() as session:
            found = sync_locations(session, client, library_id, services.settings.kavita_books_path)
    except KavitaError as exc:
        return False, str(exc)
    return True, f"{found.mapped} books located, {found.missing} not yet scanned."


@kavita_app.command("scan")
def kavita_scan() -> None:
    """Ask Kavita to scan the library and locate the books it has already read."""
    services = _services()
    try:
        ok, message = _kavita_scan(services)
    finally:
        services.close()
    if not ok:
        typer.echo(message, err=True)
        raise typer.Exit(1)
    typer.echo(message)


# --- licenses -------------------------------------------------------------------------------------


@licenses_app.command("list")
def licenses_list() -> None:
    """The class of each source and how many private documents it has."""
    services = _services()
    try:
        with services.sessions() as session:
            counts = {
                key: value
                for key, value in session.execute(
                    select(models.Document.source_id, func.count())
                    .where(models.Document.license_class == "private")
                    .group_by(models.Document.source_id)
                )
            }
            for source in session.scalars(select(models.Source).order_by(models.Source.id)):
                typer.echo(
                    f"{source.id}  {source.license_class}  "
                    f"{counts.get(source.id, 0)} private documents"
                )
    finally:
        services.close()


@licenses_app.command("blocks")
def licenses_blocks(limit: Annotated[int, typer.Option(min=1, max=200)] = 20) -> None:
    """The blocks the outputs applied (refused and filtered)."""
    services = _services()
    try:
        with services.sessions() as session:
            rows = session.scalars(
                select(models.LicenseBlock).order_by(models.LicenseBlock.id.desc()).limit(limit)
            ).all()
            for row in rows:
                when = f"{row.created_at:%Y-%m-%d %H:%M}"
                typer.echo(f"{when}  {row.output}: {row.action}, {row.items} items")
            if not rows:
                typer.echo("No blocks recorded.")
    finally:
        services.close()


@licenses_app.command("mark-private")
def licenses_mark_private(source_id: str) -> None:
    """Mark a source (a ZIM, for example) as private, persistently: it survives source
    synchronization and carries the documents along. It is only unmarked on purpose."""
    services = _services()
    try:
        with services.sessions() as session:
            try:
                changed = licenses.mark_source_private(session, source_id)
            except KeyError as exc:
                typer.echo(f"Unknown source: {source_id!r}.", err=True)
                raise typer.Exit(1) from exc
            session.commit()
            services.registry.sync(session)
    finally:
        services.close()
    typer.echo(f"{source_id}: private (persistent mark); {changed} documents changed.")


@licenses_app.command("clear-mark")
def licenses_clear_mark(
    source_id: str,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirms you have the rights to make the source open again."),
    ] = False,
) -> None:
    """Remove the \"private\" mark: the source goes back to the class it declares (a ZIM: open)."""
    if not yes:
        typer.echo(
            "Unmarking returns the source to the class it declares (open, for ZIMs): you assert "
            "that you may redistribute it. Repeat with --yes.",
            err=True,
        )
        raise typer.Exit(2)
    services = _services()
    try:
        with services.sessions() as session:
            try:
                licenses.clear_source_mark(session, source_id)
            except KeyError as exc:
                typer.echo(f"Unknown source: {source_id!r}.", err=True)
                raise typer.Exit(1) from exc
            session.commit()
            services.registry.sync(session)
    finally:
        services.close()
    typer.echo(f"{source_id}: mark removed; back to the declared class.")
