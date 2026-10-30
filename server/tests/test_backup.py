"""Fase 12: backup local (pg_dump), os comandos `ariadne backup/release` e `/api/backups`."""

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ariadne import backup
from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.cli import app as cli_app
from ariadne.config import Settings, get_settings
from ariadne.courses.repo import CourseRepo
from ariadne.db import models
from ariadne.db.migrate import head_revision
from ariadne.release import runs
from tests.coursefactory import make_course

pytestmark = pytest.mark.db
needs_pg_dump = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="precisa do pg_dump/pg_restore",
)
needs_age = pytest.mark.skipif(shutil.which("age") is None, reason="precisa do binário age")


def libpq(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def settings(database_url: str, tmp_path: Path) -> Settings:
    repo = CourseRepo(tmp_path / "cursos")
    repo.save(make_course("curso-aberto"))
    return Settings(
        database_url=database_url,
        zim_dir=tmp_path / "zim",
        courses_repo=repo.path,
        backup_dir=tmp_path / "backups",
        backup_extra_databases="",
        kiwix_url="http://kiwix",
    )


@pytest.fixture
def populated(db: sessionmaker[Session]) -> None:
    with db() as session, session.begin():
        session.add(
            models.Source(
                id="zim:x",
                kind="zim",
                title="X",
                version="1",
                wing="x",
                license_class="open",
                meta={},
            )
        )
        session.add(models.Principal(name="alguem", kind="user", role="admin"))
        session.add(
            models.TranslationCache(
                key="k", source_lang="en", target_lang="pt", translated="olá", backend="fake"
            )
        )


@needs_pg_dump
def test_backup_local_e_um_pg_dump_completo_que_restaura(
    settings: Settings, populated: None, make_database: Callable[[], str]
) -> None:
    result = backup.create_local_backup(settings)
    names = sorted(f.name for f in result.files)
    assert (
        names == ["ariadne_test_" + names[0].split("_test_")[1], "courses.tar.gz"]
        or len(names) == 2
    )
    manifest = json.loads((result.directory / "MANIFEST.json").read_text())
    assert manifest["format"] == "ariadne-backup/1" and "private" in manifest["contains"]
    assert backup.verify_backup(result.directory) == []
    dump = next(result.directory.glob("*.dump"))
    target = make_database()
    done = subprocess.run(
        ["pg_restore", "--no-owner", "--dbname", libpq(target), str(dump)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    with psycopg.connect(libpq(target)) as conn:
        assert conn.execute("SELECT count(*) FROM source").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM principal").fetchone() == (1,)  # é completo
        # a cache de traduções existe, mas sem dados (volta a fazer-se)
        assert conn.execute("SELECT count(*) FROM translation_cache").fetchone() == (0,)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            head_revision(),
        )


@needs_pg_dump
def test_verificar_um_backup_corrompido_e_retencao(settings: Settings, populated: None) -> None:
    settings.backup_keep = 2
    first = backup.create_local_backup(settings)
    dump = next(first.directory.glob("*.dump"))
    dump.write_bytes(dump.read_bytes()[:-10] + b"corrupto!!")
    assert any("sha-256" in p for p in backup.verify_backup(first.directory))
    for _ in range(2):
        time.sleep(1.1)
        last = backup.create_local_backup(settings)
    kept = sorted(p.name for p in (settings.backup_dir / "backups").iterdir())
    assert len(kept) == 2 and last.directory.name in kept and first.directory.name not in kept


def test_so_dumpa_as_bases_extra_que_existem(settings: Settings, populated: None) -> None:
    settings.backup_extra_databases = "wikijs,moodle,nao_existe_de_certeza"
    found = backup.databases_to_dump(settings)
    assert found[0] == make_url(settings.database_url).database
    assert "nao_existe_de_certeza" not in found


# --- CLI --------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(
    settings: Settings,
    keys: tuple[str, Path],
    signing: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CliRunner]:
    for name, value in {
        "DATABASE_URL": settings.database_url,
        "BACKUP_DIR": str(settings.backup_dir),
        "BACKUP_EXTRA_DATABASES": "",
        "COURSES_REPO": str(settings.courses_repo),
        "ZIM_DIR": str(settings.zim_dir),
        "RELEASE_AGE_RECIPIENT": keys[0],
        "RELEASE_SIGNING_KEY_FILE": str(signing[0]),
        "RELEASE_SIGNING_PUBKEY": signing[1],
        "KIWIX_URL": "http://kiwix",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RELEASE_ENABLED", raising=False)
    get_settings.cache_clear()
    yield CliRunner()
    get_settings.cache_clear()


@needs_age
def test_cli_release_dry_run_build_verify_e_publicacao_desligada(
    cli_env: CliRunner, populated: None, keys: tuple[str, Path], tmp_path: Path
) -> None:
    check = cli_env.invoke(cli_app, ["release", "check"])
    assert check.exit_code == 0, check.output
    assert "publishing off" in check.output
    run = cli_env.invoke(cli_app, ["release", "run", "--dry-run"])
    assert run.exit_code == 0, run.output
    assert "[dry-run]" in run.output and "ariadne-release-" in run.output
    folder = next((tmp_path / "backups" / "releases").iterdir())
    # sem chave: só as impressões digitais
    fingerprints = cli_env.invoke(cli_app, ["release", "verify", str(folder)])
    assert (
        fingerprints.exit_code == 0
        and "Signature and fingerprints are correct" in fingerprints.output
    )
    # com a chave privada: o ensaio completo, gravado no registo
    rehearsal = cli_env.invoke(
        cli_app, ["release", "verify", str(folder), "--identity", str(keys[1]), "--record"]
    )
    assert rehearsal.exit_code == 0, rehearsal.output
    assert "restore into a temporary database" in rehearsal.output
    assert "Restore rehearsal completed" in rehearsal.output
    # publicar sem RELEASE_ENABLED é recusado
    refused = cli_env.invoke(cli_app, ["release", "publish", str(folder)])
    assert refused.exit_code == 1 and "Publishing is off" in refused.output
    status = cli_env.invoke(cli_app, ["release", "status"])
    assert status.exit_code == 0 and "rehearsal: ok" in status.output


@needs_pg_dump
def test_cli_backup_create_local_regista_e_release_nao_cifra(
    cli_env: CliRunner, populated: None, tmp_path: Path, db: sessionmaker[Session]
) -> None:
    created = cli_env.invoke(cli_app, ["backup", "create"])
    assert created.exit_code == 0, created.output
    assert "Contains everything" in created.output
    with db() as session:
        row = runs.latest(session, "backup")
        assert row and row.status == "ok" and row.size_bytes and row.name.startswith("backup-")
    verified = cli_env.invoke(
        cli_app, ["backup", "verify", str(next((tmp_path / "backups" / "backups").iterdir()))]
    )
    assert verified.exit_code == 0 and "intact" in verified.output
    preview = cli_env.invoke(cli_app, ["backup", "create", "--destination", "release"])
    assert preview.exit_code == 0 and "UNENCRYPTED" in preview.output
    assert cli_env.invoke(cli_app, ["backup", "create", "--destination", "x"]).exit_code == 2


# --- API --------------------------------------------------------------------------------------


class Keys:
    admin: str
    member: str


@pytest.fixture
def api(
    database_url: str,
    db: sessionmaker[Session],
    settings: Settings,
    keys: tuple[str, Path],
    signing: tuple[Path, str],
) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, admin = create_principal(session, PrincipalKind.user, "admin", Role.admin)
        _, member = create_principal(session, PrincipalKind.user, "membro")
    settings.release_age_recipient = keys[0]
    settings.release_signing_key_file = str(signing[0])
    settings.release_signing_pubkey = signing[1]
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {admin}"}) as client:
        keys = Keys()
        keys.admin, keys.member = admin, member
        client.keys = keys  # type: ignore[attr-defined]
        yield client


def test_api_backups_vazio_e_estado_do_agendamento(api: TestClient) -> None:
    body = api.get("/api/backups").json()
    assert body["last_release"] is None and body["last_backup"] is None and body["history"] == []
    assert body["schedule"] == {
        "enabled": False,
        "recipient_configured": True,
        "interval_days": 7.0,
        "next_due": None,
        "due_now": False,
        "reason": "off",
        "build_pending": False,
    }
    assert body["rehearsal"]["overdue"] is False  # sem releases, nada em atraso


def test_api_construir_ensaio_e_estado(
    api: TestClient, db: sessionmaker[Session], populated: None
) -> None:
    member = {"Authorization": f"Bearer {api.keys.member}"}  # type: ignore[attr-defined]
    assert api.get("/api/backups", headers=member).status_code == 200
    assert api.post("/api/backups/release", headers=member).status_code == 403
    queued = api.post("/api/backups/release")
    assert queued.status_code == 202
    assert api.post("/api/backups/release").json()["job_id"] == queued.json()["job_id"]
    assert api.get("/api/backups").json()["schedule"]["build_pending"] is True
    # um ensaio registado à mão numa release que existe
    with db() as session, session.begin():
        row = runs.start(session, kind="release", name="ariadne-release-20261101T000000Z")
        row.status, row.size_bytes, row.parts = "ok", 12_345, 3
        row.details = {"counts": {"sources": 4, "wiki_pages": 10, "courses": 1}}
        run_id = row.id
    state = api.get("/api/backups").json()
    assert (
        state["last_release"]["parts"] == 3 and state["last_release"]["counts"]["wiki_pages"] == 10
    )
    assert state["rehearsal"]["overdue"] is True and state["rehearsal"]["last_ok_at"] is None
    rehearsed = api.post(
        f"/api/backups/{run_id}/rehearsal", json={"status": "ok", "notes": "no Mac"}
    )
    assert rehearsed.status_code == 200 and rehearsed.json()["rehearsal_status"] == "ok"
    assert api.get("/api/backups").json()["rehearsal"]["overdue"] is False
    assert (
        api.post(
            f"/api/backups/{run_id}/rehearsal", json={"status": "ok"}, headers=member
        ).status_code
        == 403
    )
    assert api.post("/api/backups/999/rehearsal", json={"status": "ok"}).status_code == 404
    # um ensaio com mais de 90 dias volta a estar em atraso
    with db() as session, session.begin():
        row = session.get(models.BackupRun, run_id)
        assert row is not None
        row.rehearsed_at = datetime.now(UTC) - timedelta(days=100)
    assert api.get("/api/backups").json()["rehearsal"]["overdue"] is True


def test_api_sem_destinatario_nao_constroi(api: TestClient) -> None:
    api.app.state.services.settings.release_age_recipient = ""  # type: ignore[attr-defined]
    response = api.post("/api/backups/release")
    assert response.status_code == 409 and "RELEASE_AGE_RECIPIENT" in response.json()["detail"]
    with api.app.state.sessionmaker() as session:  # type: ignore[attr-defined]
        assert (
            session.scalar(select(models.Job.id).where(models.Job.kind == "release_build")) is None
        )


@needs_pg_dump
def test_o_prune_so_apaga_dentro_do_backup_dir_nunca_no_out(
    settings: Settings, populated: None, tmp_path: Path
) -> None:
    settings.backup_keep = 1
    elsewhere = tmp_path / "fora"
    for _ in range(2):
        backup.create_local_backup(settings, elsewhere)
        time.sleep(1.1)
    assert len(list(elsewhere.glob("backup-*"))) == 2  # nunca se limpa um --out alheio
    inside = settings.backup_dir / "backups"
    for _ in range(2):
        backup.create_local_backup(settings)
        time.sleep(1.1)
    assert len(list(inside.glob("backup-*"))) == 1
    stranger = tmp_path / "fora" / "backup-nao-meu"
    stranger.mkdir()
    assert backup.prune(elsewhere, "backup-", 0, base=settings.backup_dir) == []
    assert stranger.is_dir()


@needs_age
def test_a_pre_visualizacao_da_release_e_so_do_dono_e_fica_so_a_ultima(
    cli_env: CliRunner, populated: None, tmp_path: Path
) -> None:
    for _ in range(2):
        done = cli_env.invoke(cli_app, ["backup", "create", "--destination", "release"])
        assert done.exit_code == 0, done.output
        time.sleep(1.1)
    previews = sorted((tmp_path / "backups" / "preview").glob("release-*"))
    assert len(previews) == 1  # as antigas apagam-se: é texto claro
    root = previews[0]
    assert root.stat().st_mode & 0o777 == 0o700
    files = [p for p in root.rglob("*") if p.is_file()]
    assert files and all(p.stat().st_mode & 0o777 == 0o600 for p in files)
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in root.rglob("*") if p.is_dir())


def test_api_agentes_nao_veem_o_erro_nem_quem_pediu_e_o_estado_preso_corrige_se(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    with db() as session, session.begin():
        _, agent = create_principal(session, PrincipalKind.agent, "agente")
        failed = runs.start(session, kind="release", name="ariadne-release-x", requested_by="dono")
        failed.status, failed.error = "failed", "segredo do erro"
        stuck = runs.start(session, kind="backup", name="backup-preso", requested_by="cli")
        stuck.started_at = datetime.now(UTC) - timedelta(hours=9)
        stuck_id = stuck.id
    headers = {"Authorization": f"Bearer {agent}"}
    seen = api.get("/api/backups", headers=headers).json()
    for run in (seen["last_release"], *seen["history"]):
        assert run["error"] is None and run["requested_by"] is None
    mine = api.get("/api/backups").json()
    assert mine["last_release"]["error"] == "segredo do erro"
    assert mine["last_release"]["requested_by"] == "dono"
    with db() as session:  # a linha «a correr» há 9 h passou a falhada
        row = session.get(models.BackupRun, stuck_id)
        assert row is not None and row.status == "failed" and "no activity" in (row.error or "")


def test_api_nao_constroi_com_a_chave_ou_a_assinatura_estragadas(api: TestClient) -> None:
    services = api.app.state.services  # type: ignore[attr-defined]
    services.settings.release_age_bin = "/usr/bin/false"  # o `age` recusa cifrar com a chave
    response = api.post("/api/backups/release")
    assert response.status_code == 409 and "age failed" in response.json()["detail"]
    services.settings.release_age_bin = "age"
    services.settings.release_signing_key_file = ""
    response = api.post("/api/backups/release")
    assert response.status_code == 409 and "signed" in response.json()["detail"]
