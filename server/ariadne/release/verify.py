"""Verificar e ensaiar uma release (`ariadne release verify`).

Sem chave privada, verifica o que se pode: o manifesto, o tamanho e o sha-256 de cada parte, que o
que está nas partes é mesmo cifrado (cabeçalho age) e que nenhuma parte passa o limite. **Com** a
chave privada (`--identity`, o ficheiro fica fora do sistema) faz o ensaio completo: desencripta,
confere o sha-256 do conteúdo e o canário, repõe numa pasta temporária, compara as contagens do
`RELEASE.json` com os ficheiros, carrega o `db/release.sql` numa **base temporária** e compara as
contagens, e (com acesso à base) confirma que nada do que saiu é privado.
"""

import hashlib
import json
import secrets
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ariadne import licenses
from ariadne.db.models import BackupRun
from ariadne.release.age import HEADER, Age, AgeError
from ariadne.release.content import DATA_FILES, SCHEMA, schema_sql
from ariadne.release.pack import MANIFEST, MAX_PART_BYTES, SIGNATURE, SUMS, sha256_file
from ariadne.release.sign import SignError, verify_file
from ariadne.release.sink import ARCHIVE_ROOT

_SECRET_KEYS = ("token", "secret", "password", "api_key", "apikey", "dsn", "database_url")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyReport:
    directory: str
    checks: list[Check] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    rehearsal: bool = False  # houve desencriptação e restauro?

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, detail))
        return ok

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rehearsal": self.rehearsal,
            "counts": self.counts,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def load_manifest(directory: Path) -> dict[str, Any]:
    data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != "ariadne-release-manifest/1":
        raise ValueError("MANIFEST.json is not from an Ariadne release")
    return data


def check_signature(directory: Path, pubkey: str | None, report: VerifyReport) -> bool:
    """A autenticidade, **antes de ler o que quer que seja**: o manifesto tem de estar assinado
    pela chave pública de assinatura fixada fora do GitHub (nunca a que vem na release)."""
    if not pubkey or not pubkey.strip():
        return report.add(
            "signature", False, "the signing public key is missing (RELEASE_SIGNING_PUBKEY)"
        )
    try:
        verify_file(directory / MANIFEST, directory / SIGNATURE, pubkey)
    except SignError as exc:
        return report.add("signature", False, str(exc))
    return report.add("signature", True, "the manifest is signed by the pinned key")


def check_fingerprints(
    directory: Path, pubkey: str | None, report: VerifyReport | None = None
) -> VerifyReport:
    """O que se verifica sem a chave age: a assinatura (primeiro, e se falha não se vê mais nada),
    o manifesto e as partes (tamanho, sha-256, limite, cifradas)."""
    report = report or VerifyReport(str(directory))
    if not check_signature(directory, pubkey, report):
        return report
    try:
        manifest = load_manifest(directory)
    except (OSError, ValueError) as exc:
        report.add("manifest", False, str(exc))
        return report
    report.add("manifest", True, f"{len(manifest.get('parts', []))} part(s)")
    parts = manifest.get("parts", [])
    limit = int(manifest.get("part_max_bytes") or MAX_PART_BYTES)
    problems: list[str] = []
    for part in parts:
        path = directory / str(part["name"])
        if "/" in str(part["name"]) or not path.is_file():
            problems.append(f"{part['name']}: missing")
            continue
        size = path.stat().st_size
        if size != part["bytes"]:
            problems.append(f"{part['name']}: size {size} instead of {part['bytes']}")
        elif size > min(limit, MAX_PART_BYTES):
            problems.append(f"{part['name']}: exceeds the limit of {limit} bytes")
        elif sha256_file(path) != part["sha256"]:
            problems.append(f"{part['name']}: sha-256 does not match")
    extra = sorted(
        p.name
        for p in directory.glob("*.age.[0-9][0-9][0-9]")
        if p.name not in {x["name"] for x in parts}
    )
    if extra:
        problems.append(f"parts not in the manifest: {', '.join(extra)}")
    report.add(
        "parts", not problems and bool(parts), "; ".join(problems) or "sizes and sha-256 correct"
    )
    if parts:
        head = (directory / parts[0]["name"]).read_bytes()[: len(HEADER)] if not problems else b""
        report.add(
            "encryption",
            head == HEADER,
            "age header in the 1st part" if head == HEADER else "no age header",
        )
    sums_path = directory / SUMS
    if sums_path.is_file():
        want = {
            line.split("  ", 1)[1]: line.split("  ", 1)[0]
            for line in sums_path.read_text().splitlines()
            if "  " in line
        }
        manifest_ok = want.get(MANIFEST) == sha256_file(directory / MANIFEST)
        parts_ok = all(want.get(p["name"]) == p["sha256"] for p in parts)
        report.add(
            "SHA256SUMS",
            manifest_ok and parts_ok,
            "consistent with the manifest"
            if manifest_ok and parts_ok
            else "differs from the manifest",
        )
    else:
        report.add("SHA256SUMS", False, "missing")
    restore = directory / "restore.sh"
    want_script = manifest.get("restore_sh_sha256")
    report.add(
        "restore.sh",
        restore.is_file() and bool(want_script) and sha256_file(restore) == want_script,
        "same as the signed manifest states",
    )
    flags = manifest.get("checks", {})
    report.add("no plaintext in the parts", flags.get("plaintext_in_parts") is False, "")
    return report


# --- ensaio de restauro -----------------------------------------------------------------------


def _safe_extract(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if (
                name.startswith("/")
                or ".." in Path(name).parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"dangerous entry in the archive: {name!r}")
        tar.extractall(target, filter="data")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _restore_data(admin_url: str, tree: Path) -> dict[str, int]:
    """Carrega os `db/*.csv` numa base temporária e devolve as contagens. O ensaio **nunca** corre
    SQL do arquivo e **nunca** usa o role do `DATABASE_URL`: cria uma base e um role sem
    privilégios (sem superuser, sem criar bases nem roles), liga-se **com esse role**, cria o
    esquema (do código) e carrega os dados com `COPY FROM STDIN`; no fim apaga a base e o role."""
    url = make_url(admin_url)
    suffix = secrets.token_hex(4)
    name, role, password = (
        f"ariadne_ensaio_{suffix}",
        f"ariadne_ensaio_{suffix}",
        secrets.token_hex(16),
    )
    dsn_admin = url.set(
        drivername="postgresql", database=url.database or "postgres"
    ).render_as_string(hide_password=False)
    scratch = url.set(
        drivername="postgresql", database=name, username=role, password=password
    ).render_as_string(hide_password=False)
    with psycopg.connect(dsn_admin, autocommit=True) as admin:
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password))
        )
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(role)
            )
        )
    try:
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute(schema_sql())
            for table, relative in DATA_FILES:
                data = (tree / relative).read_bytes()
                copy = sql.SQL("COPY {}.{} FROM STDIN WITH (FORMAT csv, NULL '\\N')").format(
                    sql.Identifier(SCHEMA), sql.Identifier(table)
                )
                with conn.cursor() as cur, cur.copy(copy) as writer:
                    writer.write(data)
            got: dict[str, int] = {}
            for query, key in (
                ("SELECT count(*) FROM {}.source", "sources"),
                ("SELECT count(*) FROM {}.wiki_page", "wiki_pages"),
                ("SELECT count(DISTINCT slug) FROM {}.course", "courses"),
            ):
                row = conn.execute(sql.SQL(query).format(sql.Identifier(SCHEMA))).fetchone()
                got[key] = int(row[0]) if row else -1
        return got
    finally:
        with psycopg.connect(dsn_admin, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def rehearse(
    directory: Path,
    identity: Path,
    *,
    pubkey: str | None,
    age: Age | None = None,
    admin_database_url: str | None = None,
    session: Session | None = None,
) -> VerifyReport:
    """O ensaio de restauro completo. `identity` é o ficheiro com a chave privada (fora do
    sistema); `admin_database_url` uma base onde se pode criar uma base temporária; `session` dá
    acesso à base do Ariadne para confirmar que nada do que saiu é privado."""
    age = age or Age()
    report = check_fingerprints(directory, pubkey)
    report.rehearsal = True
    if not report.ok:
        return report
    manifest = load_manifest(directory)
    work = Path(tempfile.mkdtemp(prefix="ariadne-ensaio-"))
    try:
        archive = work / "content.tar.gz"
        joined = work / "joined.age"
        try:
            with joined.open("wb") as sink:
                for name in (p["name"] for p in manifest["parts"]):
                    with (directory / name).open("rb") as source:
                        shutil.copyfileobj(source, sink)
            with joined.open("rb") as source, archive.open("wb") as out:
                done = age.decrypt_stream(identity, source, out)
            if done.returncode != 0:
                raise AgeError(done.stderr.decode(errors="replace")[:200])
        except (AgeError, OSError) as exc:
            report.add("decrypt", False, str(exc))
            return report
        report.add("decrypt", True, "with the supplied private key")
        digest = sha256_file(archive)
        if not report.add(
            "content sha-256", digest == manifest["content_sha256"], "matches the manifest"
        ):
            return report
        root = work / "out"
        try:
            _safe_extract(archive, root)
        except (ValueError, tarfile.TarError) as exc:
            report.add("restore", False, str(exc))
            return report
        tree = root / ARCHIVE_ROOT
        report.add(
            "restore",
            tree.is_dir(),
            f"restored {sum(1 for _ in tree.rglob('*') if _.is_file())} files",
        )
        _check_tree(tree, manifest, report)
        if admin_database_url:
            _check_database(tree, admin_database_url, report)
        if session is not None:
            _check_private(tree, session, report)
            _check_registry(directory, session, report)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report


def _check_tree(tree: Path, manifest: dict[str, Any], report: VerifyReport) -> None:
    info = _read_json(tree / "RELEASE.json")
    counts = info["counts"]
    registry = _read_json(tree / "registry" / "sources.json")
    index = _read_json(tree / "wiki" / "INDEX.json")
    pages = sum(1 for _ in (tree / "wiki").rglob("*.md"))
    courses = (
        sorted(p.name for p in (tree / "courses").iterdir()) if (tree / "courses").is_dir() else []
    )
    got = {"sources": len(registry), "wiki_pages": pages, "courses": len(courses)}
    report.counts = got
    same = (
        got["sources"] == counts["sources"]
        and got["wiki_pages"] == counts["wiki_pages"] == len(index)
        and got["courses"] == counts["courses"]
    )
    report.add("file counts", same, f"{got} (RELEASE.json: {counts})")
    canary = (tree / "CANARY.txt").read_text().strip()
    report.add(
        "canary",
        hashlib.sha256(canary.encode()).hexdigest() == manifest.get("canary_sha256"),
        "the canary inside matches the manifest's",
    )
    settings = _read_json(tree / "config" / "settings.json")
    leaked = [k for k in settings if any(word in k.lower() for word in _SECRET_KEYS)]
    report.add("configs without secrets", not leaked, ", ".join(leaked) or "no suspicious name")
    report.add(
        "no user data",
        not any((tree / name).exists() for name in ("principal", "query_log", "users", "sessions")),
        "",
    )


def _check_database(tree: Path, admin_url: str, report: VerifyReport) -> None:
    info = _read_json(tree / "RELEASE.json")["counts"]
    try:
        got = _restore_data(admin_url, tree)
    except (psycopg.Error, OSError) as exc:
        report.add(
            "restore into a temporary database", False, f"{type(exc).__name__}: {str(exc)[:200]}"
        )
        return
    want = {
        "sources": info["sources"],
        "wiki_pages": info["wiki_pages"],
        "courses": info["courses"],
    }
    report.add("restore into a temporary database", got == want, f"{got} (expected {want})")


def _check_private(tree: Path, session: Session, report: VerifyReport) -> None:
    registry = _read_json(tree / "registry" / "sources.json")
    index = _read_json(tree / "wiki" / "INDEX.json")
    blocked = licenses.find_blocked(
        session,
        source_ids=[s["id"] for s in registry],
        document_ids=[p["document_id"] for p in index],
    )
    report.add(
        "nothing private in the release",
        not blocked,
        "no source or page is private" if not blocked else f"{len(blocked)} private item(s)",
    )


def _check_registry(directory: Path, session: Session, report: VerifyReport) -> None:
    """Se esta instalação conhece a release (`backup_run`), o manifesto tem de ser o mesmo que ela
    gerou: um manifesto assinado mas diferente do registado é uma release que não é a nossa."""
    manifest = load_manifest(directory)
    run_id = manifest.get("run_id")
    row = session.get(BackupRun, run_id) if isinstance(run_id, int) else None
    if row is None or row.kind != "release":
        report.add(
            "release record", True, "this installation does not know it (rehearsal of a copy)"
        )
        return
    ok = row.manifest_sha256 == sha256_file(directory / MANIFEST)
    report.add(
        "release record",
        ok,
        "the manifest is the one Ariadne recorded"
        if ok
        else "the manifest differs from the recorded one",
    )
