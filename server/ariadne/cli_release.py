"""Comandos `ariadne backup` e `ariadne release` (Fase 12)."""

import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import make_url

from ariadne import backup as backup_mod
from ariadne.backup import prune
from ariadne.config import Settings
from ariadne.db.models import BackupRun
from ariadne.licenses import LicenseBlocked
from ariadne.release import content, runs
from ariadne.release.age import Age, AgeError
from ariadne.release.publish import PublishError
from ariadne.release.run import (
    ReleaseConfigError,
    check_release_config,
    publish_built,
    release_dir,
    run_release,
)
from ariadne.release.sign import SignError, normalize_pubkey
from ariadne.release.sink import DiskSink
from ariadne.release.verify import check_fingerprints, load_manifest, rehearse
from ariadne.services import Services
from ariadne.vault.config import effective_settings

backup_app = typer.Typer(help="Local backups (full pg_dump, stays on the NAS).")
release_app = typer.Typer(
    help="Weekly external release: open class only, encrypted with age, in parts of up to 2 GB."
)


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


# --- backup -----------------------------------------------------------------------------------


@backup_app.command("create")
def backup_create(
    destination: Annotated[
        str,
        typer.Option(
            "--destination",
            help='"local" (full pg_dump of the databases + courses; never leaves the NAS) or '
            '"release" (the release\'s filtered content, unencrypted, to inspect it).',
        ),
    ] = "local",
    out: Annotated[Path | None, typer.Option("--out", help="Destination folder.")] = None,
) -> None:
    """Create a backup. The local one takes EVERYTHING (including private content and user data)."""
    settings = effective_settings()
    if destination == "release":
        services = Services.open(settings)
        try:
            parent = out or settings.backup_dir / "preview"
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = parent / f"release-{content.new_stamp()}"
            sink = DiskSink(target)  # pastas 0700, ficheiros 0600: é texto claro
            report = content.build_content(
                services,
                sink,
                canary=f"ariadne-canary-{secrets.token_hex(16)}",
                run_id=None,
                stamp=content.new_stamp(),
            )
        except LicenseBlocked as exc:
            shutil.rmtree(target, ignore_errors=True)  # nunca fica um pacote recusado a meio
            raise _fail(str(exc), 3) from exc
        finally:
            services.close()
        removed = prune(parent, "release-", 1, base=settings.backup_dir)  # fica só a última
        typer.echo(
            f"Release content (UNENCRYPTED, only the owner reads it) in {target}: {report.counts()}"
        )
        if removed:
            typer.echo(f"Old previews deleted: {', '.join(removed)}")
        typer.echo(f"Left out (counts only): {report.excluded or 'nothing'}")
        return
    if destination != "local":
        raise _fail('--destination is "local" or "release".', 2)
    services = Services.open(settings)
    try:
        with services.sessions() as session:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            run = runs.start(session, kind="backup", name=f"backup-{stamp}", requested_by="cli")
            run_id = run.id
            session.commit()
        try:
            result = backup_mod.create_local_backup(settings, out)
        except Exception as exc:
            with services.sessions() as session:
                row = session.get(BackupRun, run_id)
                if row is not None:
                    runs.finish(session, row, status="failed", error=f"{type(exc).__name__}: {exc}")
                    session.commit()
            raise _fail(f"Backup failed: {exc}", 1) from exc
        with services.sessions() as session:
            row = session.get(BackupRun, run_id)
            if row is not None:
                runs.finish(
                    session,
                    row,
                    status="ok",
                    name=result.directory.name,
                    size_bytes=result.size_bytes,
                    parts=len(result.files),
                    details={"databases": result.databases, "pg_dump": result.pg_dump},
                )
                session.commit()
    finally:
        services.close()
    typer.echo(f"Backup in {result.directory} ({result.size_bytes} bytes):")
    for file in result.files:
        typer.echo(f"  {file.name}  {file.bytes} bytes  sha256 {file.sha256[:16]}…")
    typer.echo("Contains everything, including private content: do not copy it out of the NAS.")


@backup_app.command("verify")
def backup_verify(directory: Annotated[Path, typer.Argument(help="backup-… folder")]) -> None:
    """Check the fingerprints of a local backup and that pg_restore can read the dumps."""
    problems = backup_mod.verify_backup(directory)
    for problem in problems:
        typer.echo(f"ERROR {problem}", err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo("Backup is intact.")


# --- release ----------------------------------------------------------------------------------


@release_app.command("check")
def release_check() -> None:
    """Validate the release configuration: really encrypts a byte with the configured public key,
    runs the age self-test, and signs/verifies a sample with the signing key."""
    settings = effective_settings()
    age = Age(settings.release_age_bin)
    try:
        check_release_config(settings, age)
        version = age.selftest()
    except (ReleaseConfigError, AgeError) as exc:
        raise _fail(str(exc), 1) from exc
    typer.echo(
        f"age public key valid and working (age {version}); signing key valid and "
        "equal to RELEASE_SIGNING_PUBKEY."
    )
    state = "ON" if settings.release_enabled else "off (RELEASE_ENABLED=false)"
    typer.echo(
        f"Parts of {settings.release_part_bytes} bytes (2 GB cap), "
        f"retention {settings.release_retention}, publishing {state}."
    )


@release_app.command("build")
def release_build() -> None:
    """Build the release locally (does not publish)."""
    _run(publish_it=False, dry_run=False)


@release_app.command("run")
def release_run(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Build and rehearse publishing without touching the network."
        ),
    ] = False,
) -> None:
    """Build the release and publish it (only with RELEASE_ENABLED=true) or rehearse (--dry-run)."""
    _run(publish_it=not dry_run, dry_run=dry_run)


def _run(*, publish_it: bool, dry_run: bool) -> None:
    settings = effective_settings()
    services = Services.open(settings)
    try:
        built, outcome = run_release(
            services, publish_it=publish_it, dry_run=dry_run, requested_by="cli"
        )
    except (ReleaseConfigError, AgeError, SignError, LicenseBlocked, PublishError) as exc:
        raise _fail(f"Release failed: {exc}", 1) from exc
    finally:
        services.close()
    typer.echo(f"Release {built.directory} — {built.parts} part(s), {built.size_bytes} bytes.")
    typer.echo(f"Content: {built.report.counts()} (in {built.seconds:.1f} s)")
    if outcome is not None:
        for note in outcome.notes:
            typer.echo(note)
        if outcome.url:
            typer.echo(
                f"Published: {outcome.url} (deleted: {', '.join(outcome.deleted) or 'none'})"
            )


@release_app.command("publish")
def release_publish(
    directory: Annotated[Path, typer.Argument(help="ariadne-release-… folder")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Does not touch the network.")] = False,
) -> None:
    """Publish an already built release to GitHub (off by default: RELEASE_ENABLED)."""
    settings = effective_settings()
    services = Services.open(settings)
    try:
        outcome = publish_built(services, directory, dry_run=dry_run)
    except PublishError as exc:
        raise _fail(str(exc), 1) from exc
    finally:
        services.close()
    for note in outcome.notes:
        typer.echo(note)
    if outcome.url:
        typer.echo(f"Published: {outcome.url}")
        if outcome.deleted:
            typer.echo(f"Retention: deleted {', '.join(outcome.deleted)}")


@release_app.command("verify")
def release_verify(
    directory: Annotated[
        Path, typer.Argument(help="ariadne-release-… folder (or a downloaded one)")
    ],
    identity: Annotated[
        Path | None,
        typer.Option(
            "--identity",
            "-i",
            help="File with the age private key: does the full rehearsal (decrypts and restores "
            "into a temporary database). Without it, only the signature and fingerprints are "
            "checked.",
        ),
    ] = None,
    pubkey: Annotated[
        str | None,
        typer.Option(
            "--pubkey",
            help='SIGNING public key ("ssh-ed25519 AAAA…"; default: '
            "RELEASE_SIGNING_PUBKEY). Never read from the release.",
        ),
    ] = None,
    pubkey_file: Annotated[
        Path | None, typer.Option("--pubkey-file", help=".pub file with the signing key.")
    ] = None,
    database_url: Annotated[
        str | None,
        typer.Option(
            "--database-url",
            help="Postgres where to create the rehearsal's temporary database and role (default: "
            "the one in DATABASE_URL). Only used to create them: the data is loaded with the "
            "unprivileged role.",
        ),
    ] = None,
    record: Annotated[
        bool, typer.Option("--record", help="Record the rehearsal result in the app log.")
    ] = False,
) -> None:
    """Verify a release (signature first) and, with the private key, rehearse the restore."""
    settings = _settings_or_none()
    age = Age(settings.release_age_bin if settings else "age")
    key = pubkey
    if pubkey_file is not None:
        key = (
            pubkey_file.read_text(encoding="utf-8").splitlines()[0] if pubkey_file.is_file() else ""
        )
    key = key or (settings.release_signing_pubkey if settings else "") or None
    if key:
        try:
            key = normalize_pubkey(key)
        except SignError as exc:
            raise _fail(str(exc), 2) from exc
    if identity is None:
        report = check_fingerprints(directory, key)
    else:
        admin = database_url or (_admin_url(settings) if settings else None)
        if settings is not None and (record or admin):
            services = Services.open(settings)
            try:
                with services.sessions() as session:
                    report = rehearse(
                        directory,
                        identity,
                        pubkey=key,
                        age=age,
                        admin_database_url=admin,
                        session=session,
                    )
            finally:
                services.close()
        else:
            report = rehearse(directory, identity, pubkey=key, age=age, admin_database_url=admin)
    for check in report.checks:
        typer.echo(f"{'ok  ' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    if report.counts:
        typer.echo(f"restored counts: {report.counts}")
    if record and settings is not None and identity is not None:
        _record(settings, directory, report.ok, report.summary())
    if not report.ok:
        raise typer.Exit(1)
    typer.echo(
        "Restore rehearsal completed successfully."
        if identity
        else "Signature and fingerprints are correct."
    )


def _settings_or_none() -> Settings | None:
    try:
        return effective_settings()
    except ValidationError:
        return None


def _admin_url(settings: Settings) -> str:
    url = make_url(settings.database_url)
    return url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )


def _record(settings: Settings, directory: Path, ok: bool, summary: dict[str, object]) -> None:
    manifest = load_manifest(directory)
    services = Services.open(settings)
    try:
        with services.sessions() as session:
            row = session.scalar(
                select(BackupRun).where(
                    BackupRun.kind == "release", BackupRun.id == manifest.get("run_id")
                )
            )
            if row is None:
                typer.echo("Warning: this release is not in this installation's log.", err=True)
                return
            row.rehearsal_status = "ok" if ok else "failed"
            row.rehearsed_at = datetime.now(UTC)
            row.rehearsal_details = summary
            session.commit()
            typer.echo(f"Rehearsal recorded on release {row.name}.")
    finally:
        services.close()


@release_app.command("status")
def release_status() -> None:
    """The state: latest release, rehearsal and next schedule."""
    settings = effective_settings()
    services = Services.open(settings)
    try:
        with services.sessions() as session:
            last = runs.latest(session, "release")
            sched = runs.schedule(session, settings)
            _print_status(last, sched, settings)
    finally:
        services.close()


def _print_status(last: BackupRun | None, sched: runs.Schedule, settings: Settings) -> None:
    typer.echo(f"Releases folder: {release_dir(settings)}")
    if last is None:
        typer.echo("No releases yet.")
    else:
        typer.echo(
            f"Latest: {last.name} ({last.status}) {last.started_at:%Y-%m-%d %H:%M} UTC, "
            f"{last.size_bytes or 0} bytes in {last.parts or 0} part(s); "
            f"rehearsal: {last.rehearsal_status or 'never'}"
        )
    typer.echo(
        f"Schedule: {'on' if sched.enabled else 'off'}"
        + (f", next {sched.next_due:%Y-%m-%d %H:%M} UTC" if sched.next_due else "")
        + f" ({sched.reason})"
    )
