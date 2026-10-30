"""O repositório Git dos cursos: um commit por alteração e versões que não se apagam."""

import subprocess
from pathlib import Path

import pytest

from ariadne.courses.repo import CourseNotFound, CourseRepo, CourseRepoError, version_key
from ariadne.courses.schema import Course, Lesson, TextBlock
from tests.coursefactory import make_course


def git(repo: CourseRepo, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> CourseRepo:
    return CourseRepo(tmp_path / "cursos")


def commits(repo: CourseRepo) -> list[str]:
    return git(repo, "log", "--format=%s").splitlines()


def test_guardar_cria_o_repositorio_e_faz_um_commit(repo: CourseRepo) -> None:
    result = repo.save(make_course(), "Importa o curso")

    assert result.changed and result.commit == repo.head()
    assert commits(repo) == ["Importa o curso", "Inicia o repositório de cursos"]
    assert repo.courses() == ["curso-teste"]
    assert repo.versions("curso-teste") == ["2026-09"]
    assert git(repo, "status", "--porcelain") == ""  # nada por registar


def test_guardar_o_mesmo_curso_nao_faz_commit(repo: CourseRepo) -> None:
    repo.save(make_course())
    before = commits(repo)

    again = repo.save(make_course())

    assert not again.changed and again.commit is None
    assert commits(repo) == before


def test_um_commit_por_alteracao(repo: CourseRepo) -> None:
    course = make_course()
    repo.save(course)

    lesson = (
        course.modules[0]
        .lessons[0]
        .model_copy(update={"blocks": [TextBlock(markdown="Texto novo.")]})
    )
    first = repo.update_lesson(course.id, course.version, "bases", lesson, "Altera o texto")
    second = repo.update_lesson(
        course.id, course.version, "bases", lesson.model_copy(update={"title": "Outro título"})
    )

    assert first.changed and second.changed and first.commit != second.commit
    assert commits(repo)[:2] == ["Altera a lição bases/variaveis", "Altera o texto"]
    reloaded = repo.load(course.id)
    assert reloaded.modules[0].lessons[0].title == "Outro título"
    assert git(repo, "show", "--stat", "--format=", "HEAD").count("|") == 1  # só 1 ficheiro


def test_versao_nova_nao_apaga_a_anterior(repo: CourseRepo) -> None:
    v1 = make_course(version="2026-08")
    v2 = make_course(version="2026-10", extra_lessons=True)
    repo.save(v1)
    repo.save(v2)

    assert repo.versions(v1.id) == ["2026-08", "2026-10"]
    assert repo.current_version(v1.id) == "2026-10"
    assert repo.load(v1.id, "2026-08").counts()["lessons"] == 2
    assert repo.load(v1.id, "2026-10").counts()["lessons"] == 3
    assert (repo.path / v1.id / "2026-08" / "course.yaml").is_file()


def test_guardar_uma_versao_nao_toca_nas_outras(repo: CourseRepo) -> None:
    repo.save(make_course(version="2026-08"))
    repo.save(make_course(version="2026-10"))
    before = git(repo, "rev-parse", "HEAD:curso-teste/2026-08")

    changed = make_course(version="2026-10")
    changed = Course.model_validate(
        changed.model_copy(update={"summary": "mudou"}).model_dump(mode="json")
    )
    repo.save(changed)

    assert git(repo, "rev-parse", "HEAD:curso-teste/2026-08") == before


def test_licao_que_sai_da_versao_apaga_o_ficheiro(repo: CourseRepo) -> None:
    course = make_course(extra_lessons=True)
    repo.save(course)
    smaller = Course.model_validate(
        course.model_copy(update={"modules": course.modules[:1]}).model_dump(mode="json")
    )

    result = repo.save(smaller)

    assert result.files_removed >= 2
    assert not any((repo.path / course.id / course.version).glob("*funcoes*"))
    assert repo.load(course.id).counts()["modules"] == 1


def test_historico_por_curso_e_versao(repo: CourseRepo) -> None:
    repo.save(make_course(version="2026-08"), "Versão antiga")
    repo.save(make_course(version="2026-10"), "Versão nova")

    assert [c.subject for c in repo.log("curso-teste")] == ["Versão nova", "Versão antiga"]
    assert [c.subject for c in repo.log("curso-teste", "2026-08")] == ["Versão antiga"]
    assert repo.log("outro") == []
    assert repo.last_commit("curso-teste", "2026-08") != repo.last_commit("curso-teste", "2026-10")


def test_head_le_o_repositorio_sem_processos(repo: CourseRepo) -> None:
    assert repo.head() is None  # ainda não existe
    repo.save(make_course())
    assert repo.head() == git(repo, "rev-parse", "HEAD")
    git(repo, "pack-refs", "--all")  # refs empacotadas: também se leem
    assert repo.head() == git(repo, "rev-parse", "HEAD")


def test_curso_ou_versao_inexistentes(repo: CourseRepo) -> None:
    repo.save(make_course())
    with pytest.raises(CourseNotFound):
        repo.load("nao-existe")
    with pytest.raises(CourseNotFound):
        repo.load("curso-teste", "1999-01")
    with pytest.raises(CourseNotFound):
        repo.update_lesson("curso-teste", "2026-09", "modulo-falso", Lesson(id="x", title="X"))


def test_ficheiro_estragado_diz_qual(repo: CourseRepo) -> None:
    repo.save(make_course())
    broken = next((repo.path / "curso-teste" / "2026-09").rglob("002-somar.yaml"))
    broken.write_text("id: [isto não fecha\n")

    with pytest.raises(CourseRepoError, match=r"002-somar\.yaml"):
        repo.load("curso-teste")


def test_nao_depende_da_configuracao_do_git_do_utilizador(
    repo: CourseRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "vazio"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nao-existe"))
    repo.save(make_course())

    assert git(repo, "log", "-1", "--format=%an <%ae>") == "Ariadne <ariadne@localhost>"


def test_ordem_natural_das_versoes() -> None:
    versions = ["v10", "v2", "2026-10", "2026-08", "v1"]
    assert sorted(versions, key=version_key) == ["2026-08", "2026-10", "v1", "v2", "v10"]


def test_ids_com_caminhos_nao_entram(repo: CourseRepo) -> None:
    repo.save(make_course())
    for bad in ("../fora", "a/b", ".", "..", "Curso Teste", ""):
        with pytest.raises(CourseRepoError, match="nvalid"):
            repo.load(bad)
        with pytest.raises(CourseRepoError, match="nvalid"):
            repo.versions(bad)
    with pytest.raises(CourseRepoError, match="version"):
        repo.load("curso-teste", "../../etc")
    with pytest.raises(CourseRepoError, match="version"):
        repo.log("curso-teste", "2026-09/../..")
    with pytest.raises(CourseRepoError, match="version"):
        repo.last_commit("curso-teste", "x y")
    with pytest.raises(CourseRepoError, match="nvalid"):
        repo.update_lesson("..", "2026-09", "bases", Lesson(id="x", title="X"))


def test_dois_escritores_a_arrancar_criam_um_so_repositorio(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "novo"
    errors: list[Exception] = []

    def start() -> None:
        try:
            CourseRepo(path).save(make_course())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    repo = CourseRepo(path)
    assert commits(repo).count("Inicia o repositório de cursos") == 1
    assert len(commits(repo)) == 2  # o início e o curso (os outros cinco não mudaram nada)


def test_leitura_espera_pela_escrita_em_curso(repo: CourseRepo) -> None:
    import threading
    import time

    repo.save(make_course())
    entered = threading.Event()
    release = threading.Event()
    done: list[float] = []

    def writer() -> None:
        with repo._locked():
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=writer)
    thread.start()
    entered.wait(5)

    def reader() -> None:
        started = time.monotonic()
        CourseRepo(repo.path).load("curso-teste")  # outro objeto, outro descritor do bloqueio
        done.append(time.monotonic() - started)

    reading = threading.Thread(target=reader)
    reading.start()
    time.sleep(0.4)
    assert done == []  # a leitura está à espera do escritor
    release.set()
    thread.join()
    reading.join()
    assert done and done[0] >= 0.3


def test_leituras_nao_se_bloqueiam_entre_si(repo: CourseRepo) -> None:
    repo.save(make_course())
    with repo._locked(shared=True):
        assert CourseRepo(repo.path).versions("curso-teste") == ["2026-09"]


def test_index_lock_orfao_nao_trava_os_commits(repo: CourseRepo) -> None:
    import os
    import time

    repo.save(make_course())
    lock = repo.path / ".git" / "index.lock"
    lock.write_text("")
    old = time.time() - 120
    os.utime(lock, (old, old))

    result = repo.save(make_course(version="2026-10"))

    assert result.changed and not lock.exists()


def test_index_lock_recente_nao_se_toca(repo: CourseRepo) -> None:
    repo.save(make_course())
    lock = repo.path / ".git" / "index.lock"
    lock.write_text("")  # alguém (um git à mão) está a trabalhar agora

    with pytest.raises(CourseRepoError, match=r"index\.lock"):
        repo.save(make_course(version="2026-10"))
    assert lock.exists()
