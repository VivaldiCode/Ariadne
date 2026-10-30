"""Linha de comandos dos cursos, o worker `courses` e as ligações das páginas do Wiki.js."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ariadne import queue
from ariadne.cli import app as cli
from ariadne.config import Settings, get_settings
from ariadne.courses import index
from ariadne.courses.repo import CourseRepo
from ariadne.db import models
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.services import Services
from ariadne.sources.registry import SourceRegistry
from ariadne.workers import courses as courses_worker
from ariadne.workers.importer import _link_entries
from ariadne.workers.runner import Runner
from tests.coursefactory import make_course, make_fcc_zim
from tests.fakes import default_fake
from tests.zimfactory import make_zim

pytestmark = pytest.mark.db


@pytest.fixture
def env(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, CourseRepo]]:
    zims = tmp_path / "zim"
    zims.mkdir()
    make_zim(zims / "wikipedia_pt_teste_2026-09.zim")
    make_fcc_zim(zims / "freecodecamp_en_testcurso_2026-08.zim")
    repo = CourseRepo(tmp_path / "cursos")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ZIM_DIR", str(zims))
    monkeypatch.setenv("COURSES_REPO", str(repo.path))
    monkeypatch.setenv("JOBE_URL", "")
    monkeypatch.setenv("MOODLE_URL", "")
    get_settings.cache_clear()
    yield zims, repo
    get_settings.cache_clear()


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def test_sources_classify(env: tuple[Path, CourseRepo]) -> None:
    code, out = run("sources", "classify")

    assert code == 0
    course = "zim:freecodecamp_en_testcurso: course (archive: the content comes from the extracted"
    assert course + " course)" in out
    assert "zim:wikipedia_pt_teste: article — reference with no learning path" in out


def test_importar_sincronizar_e_listar(
    env: tuple[Path, CourseRepo], db: sessionmaker[Session]
) -> None:
    code, out = run("courses", "import-fcc", "--source", "zim:freecodecamp_en_testcurso")
    assert code == 0, out
    assert "freecodecamp-testcurso 2026-08: 2 modules, 4 lessons, 4 exercises" in out
    assert "(3 unmapped)" in out and "commit " in out
    assert (
        "unmapped (1): challenge of type 0 (HTML, CSS, project or module): does not run in Node"
        in out
    )
    assert "unmapped (1): uses chai expect/should (the shim only has assert)" in out
    assert "unmapped (1): uses assert.fooBar, outside the shim's chai subset" in out

    again_code, again = run("courses", "import-fcc", "--source", "zim:freecodecamp_en_testcurso")
    assert again_code == 0 and "no changes" in again

    code, out = run("courses", "sync")
    assert code == 0, out
    assert "1 courses reindexed, 4 lessons" in out and "requires +2 -0" in out

    code, out = run("courses", "list")
    assert "freecodecamp-testcurso  versions: 2026-08  (current: 2026-08)" in out
    code, out = run("courses", "show", "freecodecamp-testcurso")
    assert code == 0 and "Curso sintético 2026-08 (CC BY-SA 4.0, open)" in out
    assert "Importa Curso sintético 2026-08" in out
    with db() as session:
        assert session.get(models.Course, "freecodecamp-testcurso") is not None


def test_importar_pede_jobe_para_verificar(env: tuple[Path, CourseRepo]) -> None:
    code, out = run(
        "courses", "import-fcc", "--source", "zim:freecodecamp_en_testcurso", "--verify"
    )

    assert code == 2 and "JOBE_URL" in out


def test_importar_de_fonte_desconhecida(env: tuple[Path, CourseRepo]) -> None:
    code, out = run("courses", "import-fcc", "--source", "zim:nao-existe")
    assert code == 1 and "Unknown source" in out


def test_publicar_sem_moodle_diz_o_que_fazer(env: tuple[Path, CourseRepo]) -> None:
    run("courses", "import-fcc", "--source", "zim:freecodecamp_en_testcurso")

    code, out = run("courses", "publish", "freecodecamp-testcurso")

    assert code == 1
    assert "MOODLE_URL and MOODLE_TOKEN" in out and "ariadne moodle bootstrap" in out


def test_moodle_bootstrap_sem_palavra_passe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOODLE_WS_PASSWORD", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MOODLE_PORT=8805\n")

    code, out = run("moodle", "bootstrap", "--env-file", str(env_file), "--wait", "1")

    assert code == 2 and "MOODLE_WS_PASSWORD" in out


def test_moodle_bootstrap_com_o_moodle_em_baixo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MOODLE_WS_PASSWORD=x\n")

    code, out = run(
        "moodle",
        "bootstrap",
        "--url",
        "http://localhost:9",
        "--env-file",
        str(env_file),
        "--wait",
        "1",
    )

    assert code == 1 and "did not give a token" in out and "docker compose logs moodle" in out
    assert "MOODLE_TOKEN" not in env_file.read_text()


# --- worker `courses` -----------------------------------------------------------------------------


@pytest.fixture
def services(db: sessionmaker[Session], tmp_path: Path) -> Iterator[Services]:
    zims = tmp_path / "zim"
    zims.mkdir()
    make_zim(zims / "wikipedia_pt_teste_2026-09.zim")
    repo = CourseRepo(tmp_path / "cursos")
    repo.save(make_course())
    settings = Settings(
        database_url="postgresql+psycopg://x@localhost/x",
        zim_dir=zims,
        courses_repo=repo.path,
        kiwix_url="http://kiwix",
    )
    registry = SourceRegistry.from_settings(settings)
    with db() as session:
        registry.sync(session)
    services = Services(settings, db, registry, default_fake().client())
    yield services
    services.close()


def job(payload: dict[str, object]) -> ClaimedJob:
    return ClaimedJob(
        id=1, kind="publish_course", payload=payload, priority=0.5, attempts=1, worker_id="t"
    )


def test_publicar_sem_moodle_configurado_adia_sem_gastar_tentativas(services: Services) -> None:
    with pytest.raises(Defer, match="MOODLE_TOKEN"):
        courses_worker.publish_job(services, job({"course": "curso-teste", "version": None}))


def test_publicar_com_payload_invalido_falha_de_vez(services: Services) -> None:
    with pytest.raises(Permanent, match="payload"):
        courses_worker.publish_job(services, job({"version": "x"}))


def test_publicar_com_o_moodle_em_baixo_adia(services: Services) -> None:
    services.settings.moodle_url = "http://localhost:9"
    services.settings.moodle_token = "token"

    with pytest.raises(Defer, match="not responding"):
        courses_worker.publish_job(services, job({"course": "curso-teste", "version": None}))


def test_pedidos_de_publicacao_nao_se_duplicam(db: sessionmaker[Session]) -> None:
    with db() as session:
        first = courses_worker.enqueue_publish(session, "curso-teste")
        second = courses_worker.enqueue_publish(session, "curso-teste")
        other = courses_worker.enqueue_publish(session, "curso-teste", "2026-08")
        session.commit()
    assert first == second and other != first


def test_o_worker_courses_reindexa_e_trata_a_fila(
    services: Services, db: sessionmaker[Session]
) -> None:
    runner = Runner(services, ["courses"])

    assert set(runner.handlers) == {"publish_course"} and "courses" in runner.periodic
    report = courses_worker.run_courses(services)

    assert report.courses == 1 and report.lessons == 2
    with db() as session:
        courses_worker.enqueue_publish(session, "curso-teste")
        session.commit()
        claimed = queue.claim(session, ["publish_course"], "w")
        session.commit()
    assert claimed is not None and claimed.payload == {"course": "curso-teste", "version": None}


# --- artigo ↔ lição nas «Ligações» do Wiki.js -----------------------------------------------------


def test_as_ligacoes_de_um_artigo_apontam_a_licao_para_o_moodle(
    services: Services, db: sessionmaker[Session]
) -> None:
    index.sync_courses(services)
    article = "zim:wikipedia_pt_teste/Lisboa"
    lesson = "course:curso-teste/bases/somar"
    from ariadne.documents import DocumentInfo, upsert_documents
    from ariadne.graph import canonical

    source = services.registry.get("zim:wikipedia_pt_teste")
    assert source is not None
    with db() as session:
        upsert_documents(session, [DocumentInfo(source, "Lisboa", "Lisboa")])
        src, dst = canonical(article, lesson)
        session.add(
            models.Link(
                src_document=src, dst_document=dst, kind="vector", score=0.95, status="accepted"
            )
        )
        session.execute(
            models.Document.__table__.update()
            .where(models.Document.id == lesson)
            .values(
                meta=models.Document.meta.op("||")(
                    {"moodle_url": "http://moodle/mod/quiz/view.php?id=9"}
                )
            )
        )
        session.commit()

    with db() as session:
        entries = _link_entries(session, services, article)

    [entry] = entries
    assert (entry.title, entry.url, entry.kind) == (
        "Somar dois números",
        "http://moodle/mod/quiz/view.php?id=9",
        "vector",
    )
