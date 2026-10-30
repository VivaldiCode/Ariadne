"""Formato canónico: esquema validado e ida e volta pelos ficheiros YAML."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ariadne.courses.repo import CourseRepo, normalize_course
from ariadne.courses.schema import (
    Course,
    ExerciseBlock,
    ExerciseTest,
    Lesson,
    Module,
    QuizBlock,
    QuizChoice,
    QuizQuestion,
    SolutionBlock,
    TextBlock,
)
from tests.coursefactory import exercise_lesson, make_course, text_lesson

TRICKY = [
    "",
    "linha só",
    "com tabs\there\n\tindentado",
    "CRLF\r\ndos\r\nwindows\r\n",
    "espaços no fim   \nde linha  ",
    "\n\nlinhas em branco antes e depois\n\n\n",
    "  recuo na primeira linha\nnormal",
    "yes",
    "1.0",
    "null",
    "- parece uma lista\n- de YAML",
    "chave: valor\n# e um comentário",
    "unicode: ção — «aspas» ☃ 日本語 \U0001f600",
    "aspas ' e \" e `crases` e \\ barras",
    "x" * 500,
    "palavra " * 80,
    "{{ twig }} {% tag %} {# nota #}",
    "```js\nconst a = 1;\n```\n\n- item\n\n> citação\n",
]


def lesson_with_texts(texts: list[str]) -> Course:
    lesson = Lesson(id="l1", title="Lição", blocks=[TextBlock(markdown=text) for text in texts])
    return Course.model_validate(
        make_course()
        .model_copy(update={"modules": [Module(id="m", title="M", lessons=[lesson])]})
        .model_dump(mode="json")
    )


def test_ida_e_volta_do_curso_completo(tmp_path: Path) -> None:
    course = normalize_course(make_course(extra_lessons=True))
    repo = CourseRepo(tmp_path / "repo")

    repo.save(course)

    assert repo.load(course.id, course.version) == course
    assert repo.load(course.id) == course  # a versão atual


@pytest.mark.parametrize("text", TRICKY)
def test_ida_e_volta_de_texto_dificil(tmp_path: Path, text: str) -> None:
    course = normalize_course(lesson_with_texts([text]))
    repo = CourseRepo(tmp_path / "repo")

    repo.save(course)
    loaded = repo.load(course.id, course.version)

    assert loaded == course
    block = loaded.modules[0].lessons[0].blocks[0]
    assert isinstance(block, TextBlock)
    expected = text.replace("\r\n", "\n").replace("\r", "\n")
    assert block.markdown == "\n".join(line.rstrip(" \t") for line in expected.split("\n"))


def test_o_markdown_fica_legivel_no_ficheiro(tmp_path: Path) -> None:
    course = lesson_with_texts(["Primeira linha\n\nSegunda linha com `código`."])
    repo = CourseRepo(tmp_path / "repo")
    repo.save(course)

    text = next((tmp_path / "repo" / course.id / course.version).rglob("001-l1.yaml")).read_text()

    assert "markdown: |-\n" in text  # bloco literal, não aspas com \n escapados
    assert "    Primeira linha\n\n    Segunda linha com `código`." in text


def test_render_e_estavel(tmp_path: Path) -> None:
    course = make_course(extra_lessons=True)
    assert CourseRepo.render(course) == CourseRepo.render(
        Course.model_validate(course.model_dump())
    )


def test_fingerprint_depende_so_do_conteudo() -> None:
    a, b = make_course(), make_course()
    changed = lesson_with_texts(["outro"])

    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != changed.fingerprint()
    assert len(a.fingerprint()) == 64


def test_campo_desconhecido_nao_carrega() -> None:
    data = make_course().model_dump(mode="json")
    data["modules"][0]["lessons"][0]["campo_novo"] = 1
    with pytest.raises(ValidationError):
        Course.model_validate(data)


def test_ids_com_caracteres_invalidos() -> None:
    with pytest.raises(ValidationError):
        Lesson(id="Tem Espaços", title="x")
    with pytest.raises(ValidationError):
        Module(id="../fora", title="x")


def test_solucao_tem_de_apontar_para_um_exercicio_da_licao() -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        Lesson(
            id="l",
            title="L",
            blocks=[SolutionBlock(exercise="nao-existe", language="javascript", code="1")],
        )


def test_exercicios_repetidos() -> None:
    ex = ExerciseBlock(id="ex", language="javascript")
    with pytest.raises(ValidationError, match="same id"):
        Lesson(id="l", title="L", blocks=[ex, ex])
    one = Module(id="m", title="M", lessons=[Lesson(id="a", title="A", blocks=[ex])])
    two = Module(id="n", title="N", lessons=[Lesson(id="b", title="B", blocks=[ex])])
    data = make_course().model_dump(mode="json")
    data["modules"] = [one.model_dump(mode="json"), two.model_dump(mode="json")]
    with pytest.raises(ValidationError, match="same id in the course"):
        Course.model_validate(data)


def test_teste_nao_mapeado_diz_porque() -> None:
    with pytest.raises(ValidationError, match="gives no reason"):
        ExerciseTest(id="t1", mapped=False)
    assert ExerciseTest(id="t1", mapped=False, unmapped_reason="usa o DOM").mapped is False


def test_quiz_precisa_de_resposta_certa() -> None:
    with pytest.raises(ValidationError, match="correct answer"):
        QuizQuestion(prompt="?", choices=[QuizChoice(text="a"), QuizChoice(text="b")])
    with pytest.raises(ValidationError, match="at least two"):
        QuizQuestion(prompt="?", choices=[QuizChoice(text="a", correct=True)])
    assert QuizBlock(
        id="q",
        questions=[
            QuizQuestion(
                prompt="?", choices=[QuizChoice(text="a", correct=True), QuizChoice(text="b")]
            )
        ],
    )


def test_pre_requisitos_inexistentes_e_ciclos() -> None:
    course = make_course(extra_lessons=True).model_dump(mode="json")
    course["modules"][0]["lessons"][0]["requires"] = ["bases/nao-existe"]
    with pytest.raises(ValidationError, match="does not exist"):
        Course.model_validate(course)

    course = make_course(extra_lessons=True).model_dump(mode="json")
    course["modules"][0]["lessons"][0]["requires"] = ["funcoes/repetir"]  # repetir requer somar
    course["modules"][0]["lessons"][1]["requires"] = ["bases/variaveis"]
    with pytest.raises(ValidationError, match="cycle"):
        Course.model_validate(course)

    course = make_course().model_dump(mode="json")
    course["modules"][0]["lessons"][0]["requires"] = ["bases/variaveis"]
    with pytest.raises(ValidationError, match="requires itself"):
        Course.model_validate(course)


def test_pre_requisito_de_outro_curso_nao_se_valida_aqui() -> None:
    course = make_course().model_dump(mode="json")
    course["modules"][0]["lessons"][0]["requires"] = ["outro-curso:modulo/licao"]
    assert Course.model_validate(course).modules[0].lessons[0].requires == [
        "outro-curso:modulo/licao"
    ]


def test_versao_do_esquema_desconhecida() -> None:
    data = make_course().model_dump(mode="json")
    data["schema_version"] = 99
    with pytest.raises(ValidationError, match="not supported"):
        Course.model_validate(data)


def test_contagens() -> None:
    counts = make_course(extra_lessons=True).counts()
    assert counts == {
        "modules": 2,
        "lessons": 3,
        "exercises": 1,
        "tests": 3,
        "unmapped_tests": 0,
    }
    assert exercise_lesson().exercises()[0].id == "somar-ex"
    assert text_lesson().id == "variaveis"
