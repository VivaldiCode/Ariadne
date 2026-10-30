"""Comandos `ariadne secrets`: o cofre das integrações pela linha de comandos.

Nunca mostram um segredo guardado. `generate-key` (a chave mestra) e `generate` (um segredo que
o Ariadne inventa) imprimem o valor novo uma vez, porque é para o levar para outro lado.
"""

import getpass
import os
import sys
from typing import Annotated

import typer

from ariadne.config import get_settings
from ariadne.db.session import make_engine
from ariadne.vault import crypto
from ariadne.vault.admin import VaultAdmin
from ariadne.vault.config import IntegrationConfig
from ariadne.vault.crypto import VaultError
from ariadne.vault.registry import FIELDS
from ariadne.vault.store import DbVault

secrets_app = typer.Typer(help="Integrations vault: secrets and options encrypted in the database.")


def _actor() -> str:
    return f"cli:{getpass.getuser()}"


def _admin() -> VaultAdmin:
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    engine = make_engine(settings.database_url)
    return VaultAdmin(IntegrationConfig(settings, sessionmaker(engine)))


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"Error: {exc}", err=True)
    return typer.Exit(1)


def _key(name: str) -> str:
    key = name.strip().lower()
    if key not in FIELDS:
        raise _fail(ValueError(f"Unknown field: {name!r}. `ariadne secrets list` shows them."))
    return key


@secrets_app.command("generate-key")
def generate_key() -> None:
    """Generate a new master key (ARIADNE_MASTER_KEY). Keep it outside Ariadne (password
    manager): without it the vault's values cannot be recovered."""
    typer.echo(crypto.generate_master_key())


@secrets_app.command("status")
def status() -> None:
    """Say whether the vault works (master key present and correct) and how many values it holds."""
    state = _admin().state()
    typer.echo(f"master key: {'set' if state.has_master_key else 'missing'}")
    typer.echo(f"values in the vault: {state.entries}")
    if state.problem:
        typer.echo(f"problem: {state.problem}")
        raise typer.Exit(1)
    typer.echo("vault active")


@secrets_app.command("list")
def list_fields() -> None:
    """Each field: set or not, and where it comes from (vault, env, default). No values."""
    for integration in _admin().view():
        typer.echo(f"\n{integration.label} ({integration.id})")
        for f in integration.fields:
            where = {"vault": "vault", "env": "env", "default": "default", "none": "-"}[f.source]
            hint = f" …{f.hint}" if f.hint else ""
            typer.echo(f"  {f.env:<26} {'set' if f.defined else 'not set':<13} {where}{hint}")


@secrets_app.command("set")
def set_field(
    name: Annotated[str, typer.Argument(help="E.g. CALIBRE_API_KEY.")],
    from_env: Annotated[
        bool, typer.Option("--from-env", help="Read the value from the variable of the same name.")
    ] = False,
) -> None:
    """Save a value to the vault. Without `--from-env` it prompts silently (or reads stdin)."""
    key = _key(name)
    if from_env:
        value = os.environ.get(key.upper(), "")
    elif sys.stdin.isatty():
        value = getpass.getpass(f"{key.upper()}: ")
    else:
        value = sys.stdin.read()
    try:
        _admin().set_value(key, value, _actor())
    except VaultError as exc:
        raise _fail(exc) from exc
    typer.echo(f"{key.upper()} saved to the vault.")


@secrets_app.command("delete")
def delete_field(name: Annotated[str, typer.Argument()]) -> None:
    """Remove the value from the vault (the environment variable applies again, if any)."""
    try:
        removed = _admin().delete(_key(name), _actor())
    except VaultError as exc:
        raise _fail(exc) from exc
    typer.echo("Deleted." if removed else "It was not in the vault.")


@secrets_app.command("generate")
def generate(name: Annotated[str, typer.Argument(help="E.g. CALIBRE_API_KEY.")]) -> None:
    """Invent a random value for one of our own secrets and print it, this one time only."""
    try:
        value = _admin().generate(_key(name), _actor())
    except VaultError as exc:
        raise _fail(exc) from exc
    typer.echo(value)


@secrets_app.command("import-env")
def import_env(
    overwrite: Annotated[
        bool, typer.Option(help="Replace what is already in the vault (default: keep).")
    ] = False,
    dry_run: Annotated[bool, typer.Option(help="Only show what it would do.")] = False,
) -> None:
    """Copy into the vault what is now in the container's environment variables. Afterwards you
    can remove the secrets from Docker (ARIADNE_MASTER_KEY, DATABASE_URL and
    POSTGRES_PASSWORD stay)."""
    try:
        report = _admin().import_env(_actor(), overwrite=overwrite, dry_run=dry_run)
    except VaultError as exc:
        raise _fail(exc) from exc
    verb = "Would copy" if dry_run else "Copied"
    typer.echo(
        f"{verb} to the vault ({len(report['imported'])}): {', '.join(report['imported']) or '-'}"
    )
    typer.echo(
        f"Already in the vault, kept ({len(report['kept'])}): {', '.join(report['kept']) or '-'}"
    )
    typer.echo(f"No value in the environment: {len(report['empty'])}")


@secrets_app.command("rekey")
def rekey() -> None:
    """Master key rotation: re-encrypt everything with the new key. The current one comes from
    ARIADNE_MASTER_KEY and the new one from ARIADNE_NEW_MASTER_KEY; then change the value in
    Docker."""
    new = os.environ.get("ARIADNE_NEW_MASTER_KEY", "")
    if not new:
        raise _fail(ValueError("Set ARIADNE_NEW_MASTER_KEY (ariadne secrets generate-key)."))
    try:
        count = _admin().vault.rekey(new, _actor())
    except VaultError as exc:
        raise _fail(exc) from exc
    typer.echo(f"{count} values re-encrypted. Now put the new key in ARIADNE_MASTER_KEY.")


@secrets_app.command("reset")
def reset(
    yes: Annotated[bool, typer.Option("--yes", help="Confirm.")] = False,
) -> None:
    """Delete the WHOLE vault. Only for when the master key was lost: the stored values cannot
    be recovered; afterwards you set the new key and put the secrets back (dashboard or `set`)."""
    if not yes:
        raise _fail(ValueError("This deletes every value in the vault. Repeat with --yes."))
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    vault = DbVault(sessionmaker(make_engine(settings.database_url)), settings.ariadne_master_key)
    typer.echo(f"{vault.wipe(_actor())} values deleted from the vault.")


@secrets_app.command("audit")
def audit(limit: Annotated[int, typer.Option(help="Lines.")] = 30) -> None:
    """Who changed what and when (no values)."""
    for e in _admin().vault.audit_log(limit):
        typer.echo(
            f"{e.at:%Y-%m-%d %H:%M}  {e.actor:<18} {e.action:<9} {e.key or '-'}  {e.detail or ''}"
        )
