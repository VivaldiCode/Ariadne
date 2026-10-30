"""Backup local (`ariadne backup create`; STRUCTURE.md, secção 16; Fase 12).

Tudo vive no NAS e os snapshots ZFS cobrem os volumes; isto acrescenta um `pg_dump` lógico
(formato custom, restaura-se com `pg_restore`) da base do Ariadne, da do Wiki.js e, se existir, da
do Moodle, mais uma cópia do repositório dos cursos canónicos. É um backup **completo**: leva o
conteúdo privado e os dados de utilizadores, por isso **nunca sai do NAS** (nem se encripta
para fora): a release externa é outra coisa (`ariadne.release`, só a classe aberto). Da base do
Ariadne só se deixa de fora os dados da cache de traduções (volta a fazer-se).

Para a release (`--destination release`) o `pg_dump` não serve: não filtra linhas por classe de
licença. Esse destino escreve, sem encriptar, o mesmo conteúdo filtrado que a release leva
(`ariadne.release.content`), para o inspecionar antes de a encriptar.
"""

import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy.engine import URL, make_url

from ariadne import __version__
from ariadne.config import Settings
from ariadne.release.pack import sha256_file

EXCLUDE_DATA = ("translation_cache",)
DUMP_TIMEOUT = 6 * 3600


class BackupError(Exception):
    pass


@dataclass
class BackupFile:
    name: str
    bytes: int
    sha256: str
    kind: str  # dump | courses


@dataclass
class BackupResult:
    directory: Path
    files: list[BackupFile] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)
    pg_dump: str = ""

    @property
    def size_bytes(self) -> int:
        return sum(f.bytes for f in self.files)


def _libpq(url: URL, database: str) -> URL:
    return url.set(drivername="postgresql", database=database)


def databases_to_dump(settings: Settings) -> list[str]:
    """A base do Ariadne e as do Wiki.js e do Moodle que existirem no mesmo servidor."""
    url = make_url(settings.database_url)
    main = url.database or "ariadne"
    wanted = [d.strip() for d in settings.backup_extra_databases.split(",") if d.strip()]
    if not wanted:
        return [main]
    with psycopg.connect(
        _libpq(url, main).render_as_string(hide_password=False), connect_timeout=10
    ) as conn:
        present = {
            row[0]
            for row in conn.execute("SELECT datname FROM pg_database WHERE NOT datistemplate")
        }
    return [main, *[d for d in wanted if d in present and d != main]]


def pg_dump_version() -> str:
    binary = shutil.which("pg_dump")
    if binary is None:
        raise BackupError("`pg_dump` is not installed (postgresql-client-17).")
    out = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=False, timeout=20
    )
    return out.stdout.strip()


def _dump(url: URL, database: str, target: Path, *, exclude_data: tuple[str, ...]) -> None:
    binary = shutil.which("pg_dump")
    if binary is None:
        raise BackupError("`pg_dump` is not installed (postgresql-client-17).")
    command = [
        binary,
        "--format=custom",
        "--no-owner",
        "--no-password",
        f"--file={target}",
        f"--host={url.host or 'localhost'}",
        f"--port={url.port or 5432}",
        f"--username={url.username or 'ariadne'}",
        *[f"--exclude-table-data={t}" for t in exclude_data],
        database,
    ]
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    done = subprocess.run(
        command, capture_output=True, text=True, check=False, env=env, timeout=DUMP_TIMEOUT
    )
    if done.returncode != 0:
        target.unlink(missing_ok=True)
        raise BackupError(f"pg_dump of {database} failed: {done.stderr.strip()[:400]}")


def create_local_backup(settings: Settings, out_dir: Path | None = None) -> BackupResult:
    """O backup completo (`pg_dump` de cada base + o repositório dos cursos) numa pasta nova."""
    root = out_dir or settings.backup_dir / "backups"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"backup-{stamp}"
    work = root / f".{name}.tmp"
    final = root / name
    work.mkdir(parents=True, exist_ok=False)
    work.chmod(0o700)
    result = BackupResult(directory=final, pg_dump=pg_dump_version())
    url = make_url(settings.database_url)
    try:
        main = url.database or "ariadne"
        result.databases = databases_to_dump(settings)
        for database in result.databases:
            target = work / f"{database}.dump"
            _dump(url, database, target, exclude_data=EXCLUDE_DATA if database == main else ())
            result.files.append(
                BackupFile(target.name, target.stat().st_size, sha256_file(target), "dump")
            )
        repo = settings.courses_repo
        if repo.is_dir() and any(repo.iterdir()):
            target = work / "courses.tar.gz"
            with tarfile.open(target, "w:gz") as tar:
                tar.add(repo, arcname="courses")
            result.files.append(
                BackupFile(target.name, target.stat().st_size, sha256_file(target), "courses")
            )
        manifest: dict[str, Any] = {
            "format": "ariadne-backup/1",
            "created_at": stamp,
            "ariadne_version": __version__,
            "pg_dump": result.pg_dump,
            "databases": result.databases,
            "excluded_data": list(EXCLUDE_DATA),
            "contains": "EVERYTHING, including private content and user data: "
            "never leaves the NAS (STRUCTURE.md, section 16)",
            "files": [
                {"name": f.name, "bytes": f.bytes, "sha256": f.sha256, "kind": f.kind}
                for f in result.files
            ],
            "restore": "pg_restore --clean --if-exists --no-owner -d <base> <ficheiro>.dump",
        }
        (work / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        work.rename(final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    prune(root, "backup-", settings.backup_keep, base=settings.backup_dir)
    return result


def prune(root: Path, prefix: str, keep: int, *, base: Path) -> list[str]:
    """Apaga as pastas `prefix*` mais antigas, deixando as últimas `keep` — **só dentro de `base`**
    (o `BACKUP_DIR`): um `--out` noutro sítio nunca é limpo por nós."""
    try:
        root.resolve().relative_to(base.resolve())
    except ValueError:
        return []
    found = sorted((p for p in root.glob(f"{prefix}*") if p.is_dir()), key=lambda p: p.name)
    removed: list[str] = []
    for old in found[: max(0, len(found) - max(keep, 1))]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed


def verify_backup(directory: Path) -> list[str]:
    """Os problemas de um backup local (vazio = bom): impressões digitais e `pg_restore --list`."""
    problems: list[str] = []
    try:
        manifest = json.loads((directory / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"unreadable manifest: {exc}"]
    pg_restore = shutil.which("pg_restore")
    for entry in manifest.get("files", []):
        path = directory / entry["name"]
        if not path.is_file() or sha256_file(path) != entry["sha256"]:
            problems.append(f"{entry['name']}: missing or the sha-256 does not match")
        elif entry["kind"] == "dump" and pg_restore:
            done = subprocess.run(
                [pg_restore, "--list", str(path)],
                capture_output=True,
                check=False,
                timeout=600,
            )
            if done.returncode != 0:
                problems.append(f"{entry['name']}: pg_restore cannot read it")
    return problems
