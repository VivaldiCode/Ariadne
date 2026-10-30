"""Fase 12: a release externa — conteúdo filtrado, age, partes, manifesto, restauro e publicação."""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from ariadne.config import Settings
from ariadne.db import models
from ariadne.licenses import LicenseBlocked
from ariadne.release import content, runs
from ariadne.release.age import Age, AgeError, check_recipient
from ariadne.release.pack import MANIFEST
from ariadne.release.publish import SOURCE_REPO, PublishError, ReleaseDisabled, publish
from ariadne.release.run import ReleaseConfigError, build_release, run_release
from ariadne.release.sink import DiskSink
from ariadne.release.verify import check_fingerprints, rehearse
from ariadne.services import Services
from tests.githubfake import REPO, TOKEN, FakeGitHub, serve_github
from tests.releasekit import (
    OPEN_BODY,
    OPEN_SOURCE,
    SECRET_BODY,
    SECRET_TITLE,
    USER_NAME,
    needs_tools,
    tree_text,
)

CREDENCIAIS = "utilizador:palavra-longa"  # num URL de teste: tem de ser redigida das saídas

pytestmark = [pytest.mark.db, needs_tools]


def to_disk(services: Services, folder: Path, **kw: Any) -> content.ContentReport:
    kw.setdefault("canary", "canario-123")
    kw.setdefault("run_id", 1)
    kw.setdefault("stamp", "20261105T000000Z")
    return content.build_content(services, DiskSink(folder), **kw)


def pg_env(url: str) -> tuple[dict[str, str], str]:
    parsed = make_url(url)
    env = {
        **os.environ,
        "PGHOST": parsed.host or "localhost",
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": parsed.username or "",
        "PGPASSWORD": parsed.password or "",
    }
    return env, str(parsed.database)


def pub(services: Services) -> str:
    return services.settings.release_signing_pubkey


# --- o conteúdo -------------------------------------------------------------------------------


def test_a_release_so_leva_a_classe_aberto(services: Services, tmp_path: Path) -> None:
    out = tmp_path / "conteudo"
    report = to_disk(services, out)
    assert report.counts()["sources"] == 1  # só a ZIM aberta está na tabela `source`
    assert report.counts()["wiki_pages"] == 2 and report.counts()["courses"] == 2
    assert sorted(p.name for p in (out / "wiki" / "aberta_pt" / "pt").iterdir()) == [
        "lisboa.md",
        "porto.md",
    ]
    assert (out / "courses" / "curso-aberto").is_dir()
    assert not (out / "courses" / "curso-privado").exists()
    everything = tree_text(out)
    for forbidden in (
        SECRET_BODY, SECRET_TITLE, "book:reservado", "zim:marcada", "Livro comprado",
        USER_NAME, "hash-secreto-de-chave", "curso-privado", "TOKEN-SECRETO-DO-WIKI",
        "CHAVE-SECRETA-DO-CURADOR", "dono@example.org", "origem-privada", "nao-sai.zim",
    ):  # fmt: skip
        assert forbidden not in everything, forbidden
    page = (out / "wiki/aberta_pt/pt/lisboa.md").read_text()
    assert OPEN_BODY in page and "[Porto](/aberta_pt/pt/porto)" in page
    assert report.excluded["fontes_privadas"] == 2 and report.excluded["documentos_privados"] >= 1
    assert (
        report.excluded["cursos_privados"] == 1
        and report.excluded["versoes_privadas_de_cursos"] == 2
    )
    with services.sessions() as session:
        blocks = session.scalars(select(models.LicenseBlock)).all()
    assert any(b.output == "release externa" and b.action == "filtered" for b in blocks)


def test_so_as_versoes_abertas_de_um_curso_saem(services: Services, tmp_path: Path) -> None:
    out = tmp_path / "c"
    report = to_disk(services, out)
    misto = out / "courses" / "curso-misto"
    assert [p.name for p in misto.iterdir()] == ["2026-09"]  # a 2026-10 é privada
    assert "Versão reservada" not in tree_text(out)
    assert report.excluded["versoes_privadas_de_cursos"] == 2  # o `curso-privado` e a versão nova
    assert report.shipped_courses[("curso-misto", "2026-09")] == "open"


def test_a_marca_privada_a_mao_de_um_curso_vale_para_todas_as_versoes(
    services: Services, db: sessionmaker[Session], tmp_path: Path
) -> None:
    with db() as session, session.begin():
        session.add(
            models.Source(
                id="course:curso-aberto", kind="course", title="x", version="1", wing="x",
                license_class="open", license_override="private", meta={},
            )
        )  # fmt: skip
    out = tmp_path / "c"
    to_disk(services, out)
    assert not (out / "courses" / "curso-aberto").exists()


def test_a_verificacao_final_apanha_um_ficheiro_de_curso_privado_que_um_filtro_deixasse_passar(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ariadne.courses.schema import Course

    # Um filtro estragado: o carregamento diz que a versão privada é aberta…
    real = content.CourseRepo.load

    def lying(self: Any, slug: str, version: str | None = None) -> Course:
        course = real(self, slug, version)
        course.license.license_class = content.LicenseClass.OPEN
        return course

    monkeypatch.setattr(content.CourseRepo, "load", lying)
    # …mas a barreira final lê a licença do `course.yaml` que ia no pacote.
    with pytest.raises(LicenseBlocked) as caught:
        to_disk(services, tmp_path / "c")
    assert {b.id for b in caught.value.blocked} == {"curso-privado/2026-09", "curso-misto/2026-10"}


def test_nenhuma_tabela_de_utilizadores_e_lida(services: Services, tmp_path: Path) -> None:
    statements: list[str] = []
    engine = services.sessions.kw["bind"]

    @event.listens_for(engine, "before_cursor_execute")
    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    to_disk(services, tmp_path / "c")
    event.remove(engine, "before_cursor_execute", capture)
    joined = "\n".join(statements).lower()
    for table in (
        "principal",
        "query_log",
        "query_result",
        "tool_call",
        "translation_cache",
        "job",
    ):
        assert not re.search(rf"\b{table}\b", joined), table
    assert "document" in joined


def test_a_verificacao_final_recusa_o_que_um_filtro_deixasse_passar(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leaky(
        session: Session,
    ) -> list[Any]:  # um filtro estragado: devolve TUDO, privados incluídos
        return list(
            session.execute(
                select(models.Document, models.Curation, models.Source)
                .join(models.Curation, models.Curation.document_id == models.Document.id)
                .join(models.Source, models.Source.id == models.Document.source_id)
                .order_by(models.Document.id)
            )
        )

    monkeypatch.setattr(content, "_page_rows", leaky)
    with pytest.raises(LicenseBlocked) as caught:
        to_disk(services, tmp_path / "c")
    assert {b.id for b in caught.value.blocked} >= {
        "book:reservado/capitulo-1",
        "zim:marcada/pagina",
    }
    with services.sessions() as session:
        assert session.scalar(
            select(models.LicenseBlock).where(
                models.LicenseBlock.output == "release externa",
                models.LicenseBlock.action == "blocked",
            )
        )


def test_uma_release_falhada_nao_deixa_nada_e_fica_registada(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        content,
        "_page_rows",
        lambda session: list(
            session.execute(
                select(models.Document, models.Curation, models.Source)
                .join(models.Curation, models.Curation.document_id == models.Document.id)
                .join(models.Source, models.Source.id == models.Document.source_id)
            )
        ),
    )
    with pytest.raises(LicenseBlocked):
        build_release(services)
    root = services.settings.backup_dir / "releases"
    assert [p.name for p in root.iterdir()] == []  # nem a pasta, nem o temporário
    with services.sessions() as session:
        row = runs.latest(session, "release")
        assert row is not None and row.status == "failed" and "blocked" in (row.error or "")


def test_configuracoes_so_numa_lista_branca(tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://ariadne:palavra-passe-secreta@postgres/ariadne",
        wikijs_token="TOK-123456",
        wikijs_admin_email="dono@example.org",
        curator_api_key="KEY-123456",
        kiwix_url=f"http://{CREDENCIAIS}@kiwix:8080",
        release_age_recipient="age1abc",
        release_github_token="ghp_segredo",
        release_github_repo="dono/repo",
        zim_dir=Path("/data/segredo/zim"),
        source_class_overrides="zim:privada=book",
    )
    out = content.public_settings(settings)
    blob = json.dumps(out)
    for secret in (
        "palavra-passe-secreta", "TOK-123456", "dono@example.org", "KEY-123456", "palavra-longa",
        "ghp_segredo", "dono/repo", "/data/segredo", "kiwix:8080", "zim:privada", "postgres",
    ):  # fmt: skip
        assert secret not in blob, secret
    assert "://" not in blob and "@" not in blob
    assert out["index_daily_budget"] == 20_000 and out["embedder"] == "fake"
    assert content.settings_are_safe(out)
    assert not content.settings_are_safe({**out, "kiwix_url": "http://x"})
    assert not content.settings_are_safe({**out, "wikijs_locale": "a@b"})
    # todos os campos de texto de `Settings` ficam de fora, salvo os da lista branca
    strings = {n for n, v in settings.model_dump().items() if isinstance(v, str)}
    assert set(out) & strings <= set(content._SETTING_STRINGS)


def test_o_registo_de_fontes_so_leva_campos_seguros(services: Services, tmp_path: Path) -> None:
    out = tmp_path / "c"
    to_disk(services, out)
    registry = json.loads((out / "registry" / "sources.json").read_text())
    aberta = next(s for s in registry if s["id"] == OPEN_SOURCE)
    assert set(aberta["meta"]) == {"zim_name", "uuid"}  # nada de caminhos nem de origens privadas
    assert "origin_source_id" not in json.dumps(registry)
    assert "path" not in aberta


# --- construir, cifrar, partir, assinar, verificar, restaurar ----------------------------------


def test_constroi_partes_cifradas_com_manifesto_assinado_e_restore(
    services: Services, keys: tuple[str, Path]
) -> None:
    built = build_release(services, requested_by="teste")
    folder = built.directory
    manifest = json.loads((folder / MANIFEST).read_text())
    assert built.parts == len(manifest["parts"]) > 1
    names = sorted(p.name for p in folder.iterdir())
    for expected in ("restore.sh", "SHA256SUMS", MANIFEST, MANIFEST + ".sig"):
        assert expected in names
    for part in manifest["parts"]:
        size = (folder / part["name"]).stat().st_size
        assert size == part["bytes"] <= 1024 and re.fullmatch(
            r".*\.tar\.gz\.age\.\d{3}", part["name"]
        )
    assert manifest["encryption"]["recipient"] == keys[0] and manifest["part_max_bytes"] == 1024
    assert manifest["restore_sh_sha256"] and manifest["counts"]["wiki_pages"] == 2
    clear = (folder / MANIFEST).read_text()
    for forbidden in (SECRET_BODY, "reservado", USER_NAME, "AGE-SECRET-KEY", "fontes_privadas"):
        assert forbidden not in clear
    blob = b"".join((folder / p["name"]).read_bytes() for p in manifest["parts"])
    assert blob.startswith(b"age-encryption.org/v1")
    for plain in (b"Lisboa", b"ariadne-release", b"RELEASE.json", OPEN_BODY.encode()):
        assert plain not in blob
    report = check_fingerprints(folder, pub(services))
    assert report.ok, report.summary()
    assert report.checks[0].name == "signature"  # a assinatura vem antes de tudo
    with services.sessions() as session:
        row = runs.latest(session, "release", ok_only=True)
        assert row and row.parts == built.parts and row.size_bytes == built.size_bytes
        assert row.manifest_sha256 == built.manifest_sha256 and row.requested_by == "teste"
        assert row.details["signed"] is True


def test_nao_ha_texto_claro_no_disco_durante_nem_depois_da_construcao(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O conteúdo vai em stream para o age: nenhum ficheiro do `backup_dir` tem texto claro."""
    from ariadne.release.sink import TarSink

    root = services.settings.backup_dir
    leaks: list[str] = []
    real = TarSink.add_bytes

    def spy(self: TarSink, relative: str, data: bytes) -> None:
        if relative == "RELEASE.json":  # já passou quase tudo pelo pipe: confere-se o disco
            for file in root.rglob("*"):
                if file.is_file() and b"Lisboa" in file.read_bytes():
                    leaks.append(str(file))
        real(self, relative, data)

    monkeypatch.setattr(TarSink, "add_bytes", spy)
    built = build_release(services)
    assert leaks == []
    assert not [p for p in root.rglob("*") if p.name.startswith(".build-")]
    assert not any(b"Lisboa" in f.read_bytes() for f in root.rglob("*") if f.is_file())
    assert built.directory.is_dir()


def test_ensaio_de_restauro_com_a_chave_privada_e_um_role_sem_privilegios(
    services: Services,
    keys: tuple[str, Path],
    admin_url: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = build_release(services)
    scratch_admin = admin_url.set(drivername="postgresql").render_as_string(hide_password=False)
    users: list[str | None] = []
    real_connect = psycopg.connect

    def spy(conninfo: str = "", *args: Any, **kwargs: Any) -> Any:
        info = psycopg.conninfo.conninfo_to_dict(conninfo)
        users.append(info.get("user"))
        if str(info.get("dbname", "")).startswith("ariadne_ensaio_"):
            conn = real_connect(conninfo, *args, **kwargs)
            row = conn.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles"
                " WHERE rolname = current_user"
            ).fetchone()
            assert row == (False, False, False)  # os dados carregam-se sem privilégios
            return conn
        return real_connect(conninfo, *args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", spy)
    with services.sessions() as session:
        report = rehearse(
            built.directory, keys[1], pubkey=pub(services),
            admin_database_url=scratch_admin, session=session,
        )  # fmt: skip
    assert report.ok, report.summary()
    names = {c.name for c in report.checks}
    assert {"signature", "decrypt", "content sha-256", "canary",
            "file counts", "restore into a temporary database",
            "nothing private in the release", "release record"} <= names  # fmt: skip
    assert report.counts == {"sources": 1, "wiki_pages": 2, "courses": 2}
    assert any(str(u or "").startswith("ariadne_ensaio_") for u in users)
    with real_connect(scratch_admin) as conn:  # nada ficou: nem a base nem o role
        assert conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'ariadne_ensaio_%'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'ariadne_ensaio_%'"
        ).fetchone() == (0,)


def test_o_arquivo_nunca_executa_sql_nem_com_um_release_sql_malicioso(
    services: Services, admin_url: Any, tmp_path: Path
) -> None:
    """Um `db/release.sql` (ou meta-comandos nos dados) metido no arquivo nunca se corre."""
    from ariadne.release.verify import _restore_data

    out = tmp_path / "c"
    to_disk(services, out)
    (out / "db" / "release.sql").write_text(
        "CREATE TABLE public.pwned (x int); COPY (SELECT 1) TO PROGRAM 'touch /tmp/pwned-ariadne';"
    )
    (out / "db" / "source.csv").write_text(
        '"zim:x","zim","\\! touch /tmp/pwned-ariadne","pt","1","w","active","open","{}"\n'
    )
    scratch_admin = admin_url.set(drivername="postgresql").render_as_string(hide_password=False)
    got = _restore_data(scratch_admin, out)
    assert got["sources"] == 1  # a linha é dados: o `\!` ficou como texto
    assert not Path("/tmp/pwned-ariadne").exists()
    with psycopg.connect(scratch_admin) as conn:
        assert conn.execute("SELECT to_regclass('public.pwned')").fetchone() == (None,)


def test_a_chave_errada_nao_abre_a_release(services: Services, tmp_path: Path) -> None:
    from tests.releasekit import age_keypair

    built = build_release(services)
    _, other = age_keypair(tmp_path / "outra")
    report = rehearse(built.directory, other, pubkey=pub(services))
    assert not report.ok and any(c.name == "decrypt" and not c.ok for c in report.checks)


def test_restore_sh_verifica_a_assinatura_desencripta_e_carrega_so_dados(
    services: Services,
    keys: tuple[str, Path],
    signing: tuple[Path, str],
    tmp_path: Path,
    make_database: Callable[[], str],
) -> None:
    if shutil.which("psql") is None:
        pytest.skip("psql is not installed")
    built = build_release(services)
    target = make_database()
    env, database = pg_env(target)
    out = tmp_path / "restaurado"
    (tmp_path / "chave.pub").write_text(signing[1] + " comentario\n")
    command = [
        "bash",
        str(built.directory / "restore.sh"),
        "-k", str(tmp_path / "chave.pub"),
        "-i", str(keys[1]),
        "-o", str(out),
        "--database", database,
        "--expect-manifest-sha256", built.manifest_sha256,
    ]  # fmt: skip
    done = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=300,
        stdin=subprocess.DEVNULL, env=env,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert "signature" in done.stderr and "fingerprints" in done.stderr
    assert "Restore complete" in done.stderr and target not in done.stderr
    root = out / "ariadne-release"
    assert (root / "wiki/aberta_pt/pt/lisboa.md").is_file()
    assert (root / "courses/curso-aberto").is_dir() and not list(tmp_path.glob("ariadne-restore.*"))
    assert not (root / "db" / "release.sql").exists()  # só dados: nunca SQL no arquivo
    with psycopg.connect(target.replace("+psycopg", "")) as conn:
        assert conn.execute("SELECT count(*) FROM ariadne_release.wiki_page").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM ariadne_release.source").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM ariadne_release.course").fetchone() == (2,)
        body = conn.execute(
            "SELECT body FROM ariadne_release.wiki_page WHERE document_id = %s",
            (f"{OPEN_SOURCE}/lisboa",),
        ).fetchone()
        assert body and OPEN_BODY in body[0]


def test_adulterar_uma_parte_e_detetado_pelo_verify_pelo_restore_e_pela_publicacao(
    services: Services, keys: tuple[str, Path], tmp_path: Path
) -> None:
    built = build_release(services)
    first = sorted(built.directory.glob("*.age.001"))[0]
    data = bytearray(first.read_bytes())
    data[100] ^= 0xFF
    first.write_bytes(bytes(data))
    report = check_fingerprints(built.directory, pub(services))
    assert not report.ok and any("sha-256" in c.detail for c in report.checks)
    done = subprocess.run(
        ["bash", str(built.directory / "restore.sh"), "--pubkey", pub(services), "-i",
         str(keys[1]), "-o", str(tmp_path / "x")],
        capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    assert done.returncode == 2 and "corrupted" in done.stderr
    assert not (tmp_path / "x").exists()
    with pytest.raises(PublishError, match="verification"):
        publish(services.settings, built.directory, dry_run=True)


def test_a_chave_privada_nunca_e_aceite_como_destinatario_nem_a_pos_quantica(
    services: Services, keys: tuple[str, Path]
) -> None:
    secret = keys[1].read_text().split("AGE-SECRET-KEY-")[1].split()[0]
    with pytest.raises(AgeError, match="PRIVATE"):
        check_recipient(f"AGE-SECRET-KEY-{secret}")
    with pytest.raises(AgeError, match="valid"):
        check_recipient("não é uma chave")
    with pytest.raises(AgeError, match="valid"):  # age ≥ 1.3; a imagem traz o 1.1.1
        check_recipient("age1pq1" + "q" * 1500)
    services.settings.release_age_recipient = f"AGE-SECRET-KEY-{secret}"
    with pytest.raises(ReleaseConfigError, match="PRIVATE"):
        build_release(services)
    services.settings.release_age_recipient = keys[0]
    services.settings.release_part_bytes = 3_000_000_000
    with pytest.raises(ReleaseConfigError, match="2 GB"):
        build_release(services)


def test_o_destinatario_tem_de_cifrar_de_facto(
    services: Services, keys: tuple[str, Path], tmp_path: Path
) -> None:
    """A regex aceita-o mas o `age` desta máquina recusa-o: `check` e o build dizem-no logo."""
    fake = tmp_path / "age"
    fake.write_text("#!/bin/sh\necho 'age: chave não suportada' >&2\nexit 1\n")
    fake.chmod(0o755)
    services.settings.release_age_bin = str(fake)
    with pytest.raises(ReleaseConfigError, match="age failed"):
        build_release(services)


def test_o_autoteste_do_age_apanha_um_binario_estragado(tmp_path: Path) -> None:
    fake = tmp_path / "age"
    fake.write_text("#!/bin/sh\ncat\n")
    fake.chmod(0o755)
    shutil.copy(shutil.which("age-keygen") or "", tmp_path / "age-keygen")
    with pytest.raises(AgeError, match="did not encrypt"):
        Age(str(fake)).selftest()
    assert Age().selftest()


def test_retencao_local_so_deixa_as_ultimas(services: Services) -> None:
    services.settings.release_retention = 2
    names = []
    for _ in range(3):
        names.append(build_release(services).directory.name)
        time.sleep(1.1)
    left = sorted(p.name for p in (services.settings.backup_dir / "releases").iterdir())
    assert left == names[1:]


# --- publicação -------------------------------------------------------------------------------


def github_settings(services: Services, fake: FakeGitHub, **extra: Any) -> Settings:
    return services.settings.model_copy(
        update={
            "release_enabled": True,
            "release_github_repo": REPO,
            "release_github_token": TOKEN,
            "release_github_api_url": fake.base,
            **extra,
        }
    )


def test_dry_run_nunca_toca_na_rede_e_a_publicacao_esta_desligada(services: Services) -> None:
    fake = FakeGitHub()
    with serve_github(fake):
        built, outcome = run_release(services, publish_it=True, dry_run=True)
        assert outcome is not None and outcome.dry_run and outcome.uploaded == []
        assert any("dry-run" in note for note in outcome.notes) and fake.requests == []
        assert len(outcome.assets) == built.parts + 4  # + manifesto, assinatura, sums, restore.sh
        with pytest.raises(ReleaseDisabled):
            publish(
                services.settings.model_copy(update={"release_github_api_url": fake.base}),
                built.directory,
                dry_run=False,
            )
        assert fake.requests == []
    with services.sessions() as session:
        row = runs.latest(session, "release")
        assert row and row.published_at is None and row.details["dry_run"]


def test_publica_rascunho_envia_tudo_e_aplica_a_retencao_sem_contar_os_rascunhos_orfaos(
    services: Services,
) -> None:
    fake = FakeGitHub()
    with serve_github(fake):
        for i in range(10):  # 10 antigas nossas + 1 alheia que nunca se apaga
            fake.seed(f"ariadne-release-2025010{i % 10}T000000Z")
        orphan = fake.seed("ariadne-release-20250201T000000Z", draft=True)
        outside = fake.seed("v1.0.0")
        built = build_release(services)
        settings = github_settings(services, fake, release_retention=8)
        result = publish(settings, built.directory, dry_run=False)
        assert result.url and result.tag == built.directory.name
        mine = [r for r in fake.releases.values() if r["tag_name"] == built.directory.name]
        assert len(mine) == 1 and mine[0]["draft"] is False
        assert {a["name"] for a in mine[0]["assets"]} == {n for n, _ in result.assets}
        assert MANIFEST + ".sig" in {a["name"] for a in mine[0]["assets"]}
        ours = [r for r in fake.releases.values() if r["tag_name"].startswith("ariadne-release-")]
        assert orphan not in fake.releases and outside in fake.releases
        assert len(ours) == 8 and all(not r["draft"] for r in ours)  # o rascunho não conta
        assert len(result.deleted) == 4  # o rascunho órfão + 3 publicadas a mais
        assert all(TOKEN not in path for _, path in fake.requests)


def test_publicar_de_novo_retoma_o_rascunho_e_reenvia_o_que_ficou_incompleto(
    services: Services,
) -> None:
    fake = FakeGitHub()
    with serve_github(fake):
        built = build_release(services)
        settings = github_settings(services, fake)
        fake.fail_upload_of = "restore.sh"
        with pytest.raises(PublishError, match="500"):
            publish(settings, built.directory, dry_run=False)
        draft = next(iter(fake.releases.values()))
        assert draft["draft"] is True
        # um envio cortado a meio (tamanho errado) e outro que nunca chegou a `uploaded`
        draft["assets"][0]["size"] -= 1
        draft["assets"][1]["state"] = "starter"
        broken = {draft["assets"][0]["name"], draft["assets"][1]["name"]}
        fake.fail_upload_of = None
        result = publish(settings, built.directory, dry_run=False)
        assert len(fake.releases) == 1 and not next(iter(fake.releases.values()))["draft"]
        assert broken | {"restore.sh"} <= set(result.uploaded)
        assert len(fake.assets_deleted) == 2  # os incompletos apagaram-se antes de se reenviar
        final = {a["name"]: a for a in next(iter(fake.releases.values()))["assets"]}
        assert set(final) == {n for n, _ in result.assets}
        assert all(a["state"] == "uploaded" for a in final.values())
        with pytest.raises(PublishError, match="already published"):
            publish(settings, built.directory, dry_run=False)


def test_publicar_exige_repo_e_token(services: Services) -> None:
    built = build_release(services)
    settings = services.settings.model_copy(update={"release_enabled": True})
    with pytest.raises(PublishError, match="RELEASE_GITHUB_REPO"):
        publish(settings, built.directory, dry_run=False)
    settings = settings.model_copy(update={"release_github_repo": REPO})
    with pytest.raises(PublishError, match="TOKEN"):
        publish(settings, built.directory, dry_run=False)


def test_publicar_recusa_o_repositorio_do_codigo_do_projeto(services: Services) -> None:
    """Uma release de conteúdo nunca vai para o repositório do código, ligada ou em dry-run,
    em qualquer capitalização e sem tocar na rede."""
    built = build_release(services)
    fake = FakeGitHub()
    for name in (SOURCE_REPO, SOURCE_REPO.upper(), f" {SOURCE_REPO.title()} "):
        settings = github_settings(services, fake, release_github_repo=name)
        for dry_run in (False, True):
            with pytest.raises(PublishError, match="source repository"):
                publish(settings, built.directory, dry_run=dry_run)
    assert fake.releases == {}
    # outro repositório do mesmo dono continua a servir
    other = github_settings(services, fake, release_github_repo=SOURCE_REPO + "-conteudo")
    assert publish(other, built.directory, dry_run=True).dry_run


# --- agendamento e worker ---------------------------------------------------------------------


def test_release_e_worker_agendamento(services: Services, db: sessionmaker[Session]) -> None:
    from ariadne.workers.release import run_release_schedule

    with db() as session:
        assert not runs.schedule(session, services.settings).enabled
    assert run_release_schedule(services, threading.Event()) is None
    with db() as session:
        assert (
            session.scalar(select(models.Job.id).where(models.Job.kind == "release_build")) is None
        )
    services.settings.release_enabled = True
    with db() as session:
        due = runs.schedule(session, services.settings)
        assert due.enabled and due.due_now and due.next_due is not None
    run_release_schedule(services, threading.Event())
    run_release_schedule(services, threading.Event())
    with db() as session:
        jobs = session.scalars(select(models.Job).where(models.Job.kind == "release_build")).all()
        assert len(jobs) == 1 and jobs[0].payload["publish"] is True


def test_o_job_do_worker_constroi_e_publica(services: Services) -> None:
    from ariadne import queue
    from ariadne.workers.release import enqueue_release
    from ariadne.workers.runner import Runner

    fake = FakeGitHub()
    with serve_github(fake):
        services.settings = github_settings(services, fake)
        with services.sessions() as session:
            enqueue_release(session, publish_it=True, requested_by="agendamento")
            session.commit()
        assert Runner(services, ["release"], stop=threading.Event()).run_once()
        assert [r["draft"] for r in fake.releases.values()] == [False]
    with services.sessions() as session:
        job = session.scalar(select(models.Job).where(models.Job.kind == "release_build"))
        assert (
            job and job.status == queue.DONE and job.payload["built"].startswith("ariadne-release-")
        )
        row = runs.latest(session, "release")
        assert (
            row and row.status == "ok" and row.published_url and row.requested_by == "agendamento"
        )
        sched = runs.schedule(session, services.settings)
        assert not sched.due_now and sched.next_due is not None


def test_uma_falha_de_publicacao_reutiliza_a_pasta_ja_construida(services: Services) -> None:
    """A mesma tag: o 2.º ensaio do job só publica, não reconstrói (nem cria outra `backup_run`)."""
    from ariadne.workers.release import enqueue_release
    from ariadne.workers.runner import Runner

    fake = FakeGitHub()
    with serve_github(fake):
        services.settings = github_settings(services, fake)
        fake.fail_upload_of = "restore.sh"
        with services.sessions() as session:
            enqueue_release(session, publish_it=True, requested_by="x")
            session.commit()
        runner = Runner(services, ["release"], stop=threading.Event())
        assert runner.run_once()  # build ok, publicação falha (500): o job volta à fila com recuo
        with services.sessions() as session:
            job = session.scalar(select(models.Job).where(models.Job.kind == "release_build"))
            assert job and job.status == "pending" and job.payload["built"]
            built = job.payload["built"]
            job.run_after = job.created_at  # sem esperar pelo recuo
            session.commit()
        fake.fail_upload_of = None
        assert runner.run_once()
    root = services.settings.backup_dir / "releases"
    assert [p.name for p in root.iterdir()] == [built]  # uma só pasta: não reconstruiu
    with services.sessions() as session:
        rows = session.scalars(
            select(models.BackupRun).where(models.BackupRun.kind == "release")
        ).all()
        assert len(rows) == 1 and rows[0].published_url
    assert [r["tag_name"] for r in fake.releases.values()] == [built]


def test_o_job_respeita_a_paragem_do_worker_e_volta_a_fila(services: Services) -> None:
    from ariadne.queue import ClaimedJob, Defer
    from ariadne.workers.release import enqueue_release, release_job

    with services.sessions() as session:
        job_id = enqueue_release(session, publish_it=False, requested_by="x")
        session.commit()
    stopping = threading.Event()
    stopping.set()
    claimed = ClaimedJob(job_id, "release_build", {"publish": False}, 1, 0.0, "w")
    with pytest.raises(Defer):
        release_job(services, claimed, stopping)
    assert [p.name for p in (services.settings.backup_dir / "releases").iterdir()] == []
    with services.sessions() as session:
        row = runs.latest(session, "release")
        assert row and row.status == "failed" and "stopping" in (row.error or "")


def test_o_mesmo_release_nao_corre_duas_vezes_nem_com_o_job_a_correr(
    services: Services, db: sessionmaker[Session]
) -> None:
    from ariadne import queue
    from ariadne.workers.release import enqueue_release

    with db() as session:
        first = enqueue_release(session, publish_it=True, requested_by="a")
        session.commit()
        claimed = queue.claim(session, ["release_build"], "w1")
        session.commit()
    assert claimed is not None and claimed.id == first
    with db() as session:
        assert enqueue_release(session, publish_it=True, requested_by="b") == first


def test_sem_destinatario_o_worker_nao_agenda_nem_constroi(services: Services) -> None:
    from ariadne.queue import ClaimedJob, Permanent
    from ariadne.workers.release import enqueue_release, release_job

    services.settings.release_enabled = True
    services.settings.release_age_recipient = ""
    with services.sessions() as session:
        assert not runs.schedule(session, services.settings).due_now
        job_id = enqueue_release(session, publish_it=True, requested_by="x")
        session.commit()
    with pytest.raises(Permanent, match=r"RELEASE_AGE_RECIPIENT|key"):
        release_job(
            services,
            ClaimedJob(job_id, "release_build", {"publish": True}, 1, 0.0, "w"),
            threading.Event(),
        )


def test_uma_linha_a_correr_sem_atividade_passa_a_falhada(
    services: Services, db: sessionmaker[Session]
) -> None:
    from datetime import UTC, datetime, timedelta

    with db() as session, session.begin():
        stuck = runs.start(session, kind="release", name="ariadne-release-antiga")
        stuck.started_at = datetime.now(UTC) - timedelta(hours=7)
        fresh = runs.start(session, kind="release", name="ariadne-release-nova")
        stuck_id, fresh_id = stuck.id, fresh.id
    services.settings.release_enabled = True
    with db() as session:
        assert runs.reap_stale(session, 6.0) == 1
        session.commit()
        assert session.get(models.BackupRun, stuck_id).status == "failed"  # type: ignore[union-attr]
        assert "no activity" in (session.get(models.BackupRun, stuck_id).error or "")  # type: ignore[union-attr]
        assert session.get(models.BackupRun, fresh_id).status == "running"  # type: ignore[union-attr]
