"""Construir (e publicar) uma release: o que `ariadne release run` e o worker `release` fazem.

Construir e publicar são passos separados: a release construída fica numa pasta
`ariadne-release-<data>/` (a tag) e uma falha de publicação reutiliza-a, não reconstrói.
"""

import hashlib
import logging
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ariadne import __version__
from ariadne.backup import prune
from ariadne.config import Settings
from ariadne.db.models import BackupRun
from ariadne.release import runs
from ariadne.release.age import Age, AgeError, check_recipient
from ariadne.release.content import ContentReport, build_content, new_stamp
from ariadne.release.pack import MAX_PART_BYTES, pack, write_manifest
from ariadne.release.publish import PublishResult, publish
from ariadne.release.sign import SignError, normalize_pubkey, pubkey_of, verify_file
from ariadne.release.sink import Interrupted
from ariadne.release.verify import check_fingerprints
from ariadne.services import Services

logger = logging.getLogger(__name__)
PREFIX = "ariadne-release-"
STALE_TMP_SECONDS = 3600


class ReleaseConfigError(Exception):
    """Falta configuração (a chave pública, o age, a assinatura) ou é inválida."""


@dataclass
class BuildResult:
    run_id: int
    directory: Path
    size_bytes: int
    parts: int
    manifest_sha256: str
    report: ContentReport
    seconds: float


def release_dir(settings: Settings) -> Path:
    return settings.backup_dir / "releases"


def check_release_config(settings: Settings, age: Age | None = None) -> tuple[str, Path, str]:
    """Valida tudo o que a release precisa e devolve `(destinatário, chave de assinatura, pública)`.
    Cifra mesmo um byte com a chave configurada (o `age` desta imagem pode não aceitar um formato
    de chave que a expressão regular aceita) e assina/verifica uma amostra com a chave de
    assinatura (a pública fixada tem de ser a da privada)."""
    age = age or Age(settings.release_age_bin)
    try:
        recipient = check_recipient(settings.release_age_recipient)
        age.encrypt_bytes(recipient, b"ariadne")  # a chave serve de facto, com este binário
        if not 0 < settings.release_part_bytes <= MAX_PART_BYTES:
            raise AgeError(f"RELEASE_PART_BYTES must be between 1 and {MAX_PART_BYTES} (2 GB).")
    except AgeError as exc:
        raise ReleaseConfigError(str(exc)) from exc
    if not settings.release_signing_key_file or not settings.release_signing_pubkey:
        raise ReleaseConfigError(
            "RELEASE_SIGNING_KEY_FILE and/or RELEASE_SIGNING_PUBKEY is missing: the release "
            "must be signed (ssh-keygen -t ed25519 -f release-signing -N ''; see the README)"
        )
    key_file = Path(settings.release_signing_key_file)
    try:
        pubkey = normalize_pubkey(settings.release_signing_pubkey)
        if pubkey_of(key_file) != pubkey:
            raise SignError(
                "RELEASE_SIGNING_PUBKEY is not the public key of RELEASE_SIGNING_KEY_FILE"
            )
    except SignError as exc:
        raise ReleaseConfigError(str(exc)) from exc
    return recipient, key_file, pubkey


def cleanup_stale(root: Path) -> list[str]:
    """Apaga os temporários de construções que morreram a meio (`.ariadne-release-*.tmp`,
    `.build-*`) com mais de uma hora: podem ter partes cifradas, nunca texto claro, mas ocupam
    espaço."""
    removed: list[str] = []
    if not root.is_dir():
        return removed
    now = time.time()
    for candidate in [*root.glob(".ariadne-release-*.tmp"), *root.glob(".build-*")]:
        try:
            if now - candidate.stat().st_mtime > STALE_TMP_SECONDS:
                shutil.rmtree(candidate, ignore_errors=True)
                removed.append(candidate.name)
        except OSError:
            continue
    return removed


def build_release(
    services: Services,
    *,
    requested_by: str | None = None,
    age: Age | None = None,
    stop: threading.Event | None = None,
) -> BuildResult:
    """Constrói a release em `BACKUP_DIR/releases/ariadne-release-<data>/` (conteúdo aberto, em
    stream para o age, em partes, com manifesto assinado e restore.sh) e confere-a. Regista-a em
    `backup_run`. Com `stop`, pára a meio (`Interrupted`) se o worker for mandado parar."""
    settings = services.settings
    age = age or Age(settings.release_age_bin)
    recipient, key_file, pubkey = check_release_config(settings, age)
    started = time.monotonic()
    stamp = new_stamp()
    name = f"{PREFIX}{stamp}"
    root = release_dir(settings)
    root.mkdir(parents=True, exist_ok=True)
    cleanup_stale(root)
    with services.sessions() as session:
        runs.reap_stale(session, settings.backup_stale_hours)
        run = runs.start(session, kind="release", name=name, requested_by=requested_by)
        run_id = run.id
        session.commit()
    staging = root / f".{name}.tmp"

    def check_stop() -> None:
        if stop is not None and stop.is_set():
            raise Interrupted("the worker is stopping")

    try:
        age_version = age.selftest()
        canary = f"ariadne-canary-{secrets.token_hex(16)}"
        packed, report = pack(
            lambda sink: build_content(services, sink, canary=canary, run_id=run_id, stamp=stamp),
            staging,
            age=age,
            recipient=recipient,
            part_bytes=settings.release_part_bytes,
            base=f"{name}.tar.gz.age",
            canary=canary,
            check=check_stop,
        )
        if packed.canary_in_clear or not packed.header_ok:
            raise AgeError("O cifrado não é de confiança: o texto claro apareceu nas partes.")
        info = {
            "created_at": stamp,
            "ariadne_version": __version__,
            "run_id": run_id,
            "age_version": age_version,
            "counts": report.counts(),
            "part_max_bytes": settings.release_part_bytes,
            "canary_sha256": hashlib.sha256(canary.encode()).hexdigest(),
            "signing": {"tool": "ssh-keygen -Y", "namespace": "ariadne-release"},
            "excludes": [
                "original ZIMs",
                "model weights",
                "user data",
                "private content",
            ],
        }
        manifest, manifest_sha = write_manifest(
            staging, packed, info, recipient=recipient, signing_key=key_file
        )
        verify_file(manifest, manifest.with_name(manifest.name + ".sig"), pubkey)
        check = check_fingerprints(staging, pubkey)
        if not check.ok:
            raise AgeError(
                "The release just built does not pass verification: "
                + "; ".join(f"{c.name}: {c.detail}" for c in check.checks if not c.ok)
            )
        final = root / name
        staging.rename(final)
    except BaseException as exc:
        shutil.rmtree(staging, ignore_errors=True)
        with services.sessions() as session:
            row = session.get(BackupRun, run_id)
            if row is not None:
                reason = (
                    "interrupted (the worker was stopping)"
                    if isinstance(exc, Interrupted)
                    else (f"{type(exc).__name__}: {exc}")
                )
                runs.finish(session, row, status="failed", error=reason)
                session.commit()
        raise
    size = sum(p.bytes for p in packed.parts)
    seconds = time.monotonic() - started
    with services.sessions() as session:
        row = session.get(BackupRun, run_id)
        assert row is not None
        runs.finish(
            session,
            row,
            status="ok",
            size_bytes=size,
            parts=len(packed.parts),
            content_sha256=packed.content_sha256,
            manifest_sha256=manifest_sha,
            details={
                "counts": report.counts(),
                "excluded": report.excluded,
                "skipped": report.skipped,
                "age": age_version,
                "part_max_bytes": settings.release_part_bytes,
                "content_bytes": packed.content_bytes,
                "seconds": round(seconds, 1),
                "fingerprints": "ok",
                "signed": True,
            },
        )
        session.commit()
    removed = prune(root, PREFIX, settings.release_retention, base=settings.backup_dir)
    if removed:
        logger.info("Old local releases deleted: %s", ", ".join(removed))
    return BuildResult(run_id, final, size, len(packed.parts), manifest_sha, report, seconds)


def publish_built(
    services: Services,
    directory: Path,
    *,
    dry_run: bool,
    client: httpx.Client | None = None,
) -> PublishResult:
    """Publica (ou ensaia, com `dry_run`) uma release já construída e regista o resultado no
    `backup_run` que a construiu (pelo `run_id` do manifesto), se o houver."""
    from ariadne.release.verify import load_manifest

    run_id = load_manifest(directory).get("run_id")
    try:
        outcome = publish(services.settings, directory, dry_run=dry_run, client=client)
    except Exception as exc:
        _note(services, run_id, publish_error=f"{type(exc).__name__}: {exc}"[:500])
        raise
    if dry_run:
        _note(services, run_id, dry_run=outcome.notes)
    else:
        with services.sessions() as session:
            row = session.get(BackupRun, run_id) if isinstance(run_id, int) else None
            if row is not None:
                row.published_at = datetime.now(UTC)
                row.published_url = outcome.url
                row.details = {
                    **row.details,
                    "published": outcome.uploaded,
                    "retention_deleted": outcome.deleted,
                }
                session.commit()
    return outcome


def _note(services: Services, run_id: object, **details: object) -> None:
    if not isinstance(run_id, int):
        return
    with services.sessions() as session:
        row = session.get(BackupRun, run_id)
        if row is not None:
            row.details = {**row.details, **details}
            session.commit()


def run_release(
    services: Services,
    *,
    publish_it: bool,
    dry_run: bool,
    requested_by: str | None = None,
    age: Age | None = None,
    client: httpx.Client | None = None,
    stop: threading.Event | None = None,
) -> tuple[BuildResult, PublishResult | None]:
    """Constrói e, se pedido, publica (ou ensaia a publicação com `dry_run`)."""
    built = build_release(services, requested_by=requested_by, age=age, stop=stop)
    if not publish_it and not dry_run:
        return built, None
    return built, publish_built(services, built.directory, dry_run=dry_run, client=client)
