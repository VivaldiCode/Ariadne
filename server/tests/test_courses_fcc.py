"""Extrator do freeCodeCamp com um ZIM sintético que imita a estrutura real."""

import re
from pathlib import Path

import pytest

from ariadne.courses.coderunner import (
    PASS,
    SEPARATOR,
    is_gradable,
    mapped_tests,
    parse_results,
    question_xml,
    quiz_xml,
    render_program,
    twig_template,
)
from ariadne.courses.fcc import (
    ExtractionError,
    course_id_for,
    extract_courses,
    parse_challenge,
    parse_fences,
    parse_hints,
    slugify,
)
from ariadne.courses.fcc_tests import (
    SUPPORTED_ASSERT,
    SUPPORTED_HELPERS,
    shim_source,
    unmapped_reason,
)
from ariadne.courses.schema import ExerciseBlock, SolutionBlock, TextBlock
from ariadne.sources.zim import ZimSource
from tests.coursefactory import CHALLENGES, make_fcc_zim


@pytest.fixture
def source(tmp_path: Path) -> ZimSource:
    return ZimSource.open(make_fcc_zim(tmp_path / "freecodecamp_en_testcurso_2026-08.zim"))


def test_estrutura_do_curso(source: ZimSource) -> None:
    courses, report = extract_courses(source)

    [course] = courses
    assert course.id == "freecodecamp-testcurso"
    assert course.title == "Curso sintético"
    assert course.version == "2026-08"
    assert course.language == "en"
    assert course.programming_language == "javascript"
    assert course.summary == "Um curso pequeno para os testes. Segundo parágrafo."
    assert [m.title for m in course.modules] == ["Básicos", "Avançado"]
    assert [lesson.id for _, lesson in course.walk()] == [
        "soma-de-dois-numeros",
        "inverter-um-texto",
        "pagina-html",
        "teste-com-expect",
    ]
    assert course.origin.source_id == "zim:freecodecamp_en_testcurso"
    assert course.origin.source_version == "2026-08"
    assert course.license.name == "CC BY-SA 4.0"
    assert course.license.license_class.value == "open"
    assert (report.courses, report.lessons, report.exercises) == (1, 4, 4)


def test_blocos_de_uma_licao(source: ZimSource) -> None:
    [course], _ = extract_courses(source)
    lesson = course.modules[0].lessons[0]

    texts = [b.markdown for b in lesson.blocks if isinstance(b, TextBlock)]
    [exercise] = [b for b in lesson.blocks if isinstance(b, ExerciseBlock)]
    [solution] = [b for b in lesson.blocks if isinstance(b, SolutionBlock)]

    assert texts[0].startswith("Escreve uma função <code>soma</code>")
    assert texts[1] == "## Instructions\n\nUsa o operador `+`."
    assert exercise.language == "javascript"
    assert exercise.starter_code == "function soma(a, b) {\n  return 0;\n}"
    assert [t.description for t in exercise.tests] == [
        "`soma` deve ser uma função.",
        "`soma(1, 2)` deve devolver 3.",
        "O código não deve usar `eval`.",
    ]
    assert exercise.tests[1].code == "assert.strictEqual(soma(1, 2), 3);"
    assert solution.exercise == exercise.id
    assert "return a + b" in solution.code


def test_pre_requisitos_seguem_a_ordem_do_curriculo_dentro_do_modulo(source: ZimSource) -> None:
    [course], _ = extract_courses(source)

    assert [lesson.requires for _, lesson in course.walk()] == [
        [],
        ["basicos/soma-de-dois-numeros"],
        ["basicos/inverter-um-texto"],
        [],  # primeira do módulo seguinte
    ]
    [flat], _ = extract_courses(source, chain_requires=False)
    assert all(not lesson.requires for _, lesson in flat.walk())


def test_testes_que_mapeiam_e_os_que_nao(source: ZimSource) -> None:
    [course], report = extract_courses(source)
    by_lesson = {lesson.id: lesson.exercises()[0] for _, lesson in course.walk()}

    assert all(t.mapped for t in by_lesson["soma-de-dois-numeros"].tests)  # incl. __helpers e code
    assert all(t.mapped for t in by_lesson["inverter-um-texto"].tests)
    html = by_lesson["pagina-html"]
    assert [t.mapped for t in html.tests] == [False]
    assert "type 0" in (html.tests[0].unmapped_reason or "")
    expect = by_lesson["teste-com-expect"]
    assert [t.mapped for t in expect.tests] == [False, False, True]
    assert "expect" in (expect.tests[0].unmapped_reason or "")
    assert "assert.fooBar" in (expect.tests[1].unmapped_reason or "")
    assert sum(report.unmapped.values()) == 3
    assert not is_gradable(html) and is_gradable(expect)
    assert len(mapped_tests(expect)) == 1


def test_solucao_em_falta_nao_vira_bloco(source: ZimSource) -> None:
    [course], _ = extract_courses(source)
    lesson = next(lesson for _, lesson in course.walk() if lesson.id == "inverter-um-texto")

    assert lesson.solutions_of(lesson.exercises()[0].id) == []  # «// solution required»
    exercise = lesson.exercises()[0]
    assert exercise.harness_after == "globalThis.chamadas = 0;"  # o --before-each--


def test_titulo_dentro_de_um_bloco_de_codigo_nao_e_seccao() -> None:
    challenge = parse_challenge(CHALLENGES["avancado/ddd444"])

    assert list(challenge.sections) == ["description", "hints", "seed", "solutions"]
    assert "# --hints--" in challenge.subsections["seed"]["seed-contents"]


def test_parse_de_blocos_e_dicas() -> None:
    text = (
        "Primeira.\n\n```js\na();\n```\n\nSegunda\ncom duas linhas.\n\n"
        "````js\n```\ninterno\n```\n````\n"
    )

    assert [(f.language, f.code) for f, _, _ in parse_fences(text)] == [
        ("js", "a();"),
        ("js", "```\ninterno\n```"),
    ]
    assert parse_hints(text) == [
        ("Primeira.", "a();"),
        ("Segunda\ncom duas linhas.", "```\ninterno\n```"),
    ]


def test_desafio_invalido_e_ignorado_sem_derrubar_o_curso(tmp_path: Path) -> None:
    broken = dict(CHALLENGES)
    broken["basicos/bbb222"] = "isto não tem front matter"
    source = ZimSource.open(
        make_fcc_zim(tmp_path / "freecodecamp_en_testcurso_2026-08.zim", challenges=broken)
    )

    [course], report = extract_courses(source)

    assert course.counts()["lessons"] == 3
    assert len(report.skipped) == 1 and "bbb222" in report.skipped[0]


def test_zim_que_nao_e_do_freecodecamp(tmp_path: Path) -> None:
    from tests.zimfactory import make_zim

    with pytest.raises(ExtractionError, match=r"index\.json"):
        extract_courses(ZimSource.open(make_zim(tmp_path / "wikipedia_pt_teste_2026-09.zim")))


def test_ids_de_curso_e_slugs() -> None:
    assert course_id_for("Coding Interview Prep", "en") == "freecodecamp-coding-interview-prep"
    assert course_id_for("responsive-web-design", "pt") == "freecodecamp-responsive-web-design-pt"
    assert slugify("  Olá, Mundo! ") == "ol-mundo"
    assert slugify("???") == "x"


# --- o shim do chai e o template do CodeRunner -------------------------------------------------


def _between(shim: str, begin: str) -> str:
    start = shim.index(f"// BEGIN {begin}")
    return shim[start : shim.index(f"// END {begin}")]


def test_o_shim_define_todos_os_metodos_que_o_mapeamento_aceita() -> None:
    shim = shim_source()
    defined = set(re.findall(r"^\s{2}(\w+): ", _between(shim, "assert"), re.MULTILINE))
    helpers = set(re.findall(r"^\s{2}(\w+): ", _between(shim, "helpers"), re.MULTILINE))

    assert defined == SUPPORTED_ASSERT
    assert helpers == SUPPORTED_HELPERS


def test_o_shim_nao_tem_delimitadores_do_twig() -> None:
    assert not re.search(r"\{\{|\{%|\{#", shim_source())


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("assert.isFunction(f);", None),
        ("assert(f(1) === 2);", None),
        ("assert.strictEqual(__helpers.removeJSComments(code), 'x');", None),
        ("// assert.fooBar(1)\nassert.equal(1, 1);", None),  # comentários não contam
        ("assert.isNotNull(document.body);", "uses the browser"),
        ("expect(1).to.equal(1);", "expect"),
        ("assert.fooBar(1);", "fooBar"),
        ("__helpers.spyOn(f);", "spyOn"),
        ("import x from 'y';", "ES modules"),
        ("const fs = require('fs'); assert.ok(fs);", "require"),
        ("setTimeout(() => assert.ok(1), 10);", "timers"),
        ("const x = f(1); x === 2;", "no executable assert"),
        ("// assert.equal(f(1), 2)", "no executable assert"),
        ("fetch('/x');", "uses the browser"),
    ],
)
def test_mapeamento_de_um_teste(code: str, reason: str | None) -> None:
    result = unmapped_reason(code, 1)
    if reason is None:
        assert result is None
    else:
        assert result is not None and reason in result


def test_desafios_que_nao_sao_de_js_puro_nao_mapeiam() -> None:
    assert "type 5" in (unmapped_reason("assert.equal(1, 1);", 5) or "")


def test_o_programa_do_jobe_junta_shim_codigo_arnes_e_testes(source: ZimSource) -> None:
    [course], _ = extract_courses(source)
    lesson = course.modules[0].lessons[0]
    exercise = lesson.exercises()[0]

    program = render_program(exercise, "function soma(a, b) { return a + b; }")

    assert program.startswith("const vm = require('vm');")
    assert "class AssertionError" in program  # o shim, como texto, para o contexto
    assert '"student": "function soma(a, b) { return a + b; }"' in program
    assert program.count("assert.") >= 3 and SEPARATOR in program
    assert program.index("__SHIM") < program.index("__INPUT")


def test_resultados_do_programa() -> None:
    stdout = f"PASS\n{SEPARATOR}\nFAIL: expected 1 to equal 2\n{SEPARATOR}\nPASS\n"
    assert parse_results(stdout, 3) == [
        (True, PASS),
        (False, "FAIL: expected 1 to equal 2"),
        (True, PASS),
    ]
    assert parse_results("PASS\n", 2)[1][0] is False  # o programa acabou antes


def test_template_twig_so_interpreta_o_que_deve(source: ZimSource) -> None:
    [course], _ = extract_courses(source)
    exercise = course.modules[0].lessons[0].exercises()[0]

    template = twig_template(exercise)

    assert "{{ STUDENT_ANSWER | json_encode }}" in template
    assert "{% for TEST in TESTCASES %}" in template
    assert "{{ TEST.testcode | json_encode }}" in template
    assert template.count("STUDENT_ANSWER") == 1  # o código do aluno só entra como dados
    # o shim e o arnês vão em `verbatim`: o Twig não os toca
    shim_at = template.index("class AssertionError")
    assert (
        template.rindex("{% verbatim %}", 0, shim_at)
        < shim_at
        < template.index("{% endverbatim %}")
    )


@pytest.mark.parametrize("closer", ["{% endverbatim %}", "{%endverbatim%}", "{%- endverbatim -%}"])
def test_arnes_que_fecha_o_verbatim_e_recusado_em_qualquer_grafia(
    source: ZimSource, closer: str
) -> None:
    [course], _ = extract_courses(source)
    exercise = (
        course.modules[0]
        .lessons[0]
        .exercises()[0]
        .model_copy(update={"harness_after": f"{closer} {{{{ 7*7 }}}}"})
    )
    with pytest.raises(ValueError, match="verbatim"):
        twig_template(exercise)


def test_arnes_que_fecha_o_verbatim_e_recusado(source: ZimSource) -> None:
    [course], _ = extract_courses(source)
    exercise = (
        course.modules[0]
        .lessons[0]
        .exercises()[0]
        .model_copy(update={"harness_before": "{% endverbatim %} {{ 7*7 }}"})
    )
    with pytest.raises(ValueError, match="verbatim"):
        twig_template(exercise)


def test_xml_da_pergunta_coderunner(source: ZimSource) -> None:
    import xml.etree.ElementTree as ET

    [course], _ = extract_courses(source)
    lesson = course.modules[0].lessons[0]
    exercise = lesson.exercises()[0]

    xml = quiz_xml([question_xml(lesson, exercise)])

    root = ET.fromstring(xml)
    [question] = root.findall("question")
    assert question.get("type") == "coderunner"
    assert question.findtext("coderunnertype") == "nodejs"
    assert question.findtext("iscombinatortemplate") == "1"
    assert question.findtext("grader") == "EqualityGrader"
    assert question.findtext("memlimitmb") == "4096"
    assert question.findtext("name/text") == "Soma de dois números"
    assert "<code>soma</code>" in (question.findtext("questiontext/text") or "")
    assert question.findtext("answerpreload") == exercise.starter_code
    assert (question.findtext("answer") or "") == ""  # a solução oficial não sai do canónico
    cases = question.findall("testcases/testcase")
    assert len(cases) == 3
    assert [c.findtext("expected/text") for c in cases] == [PASS] * 3
    assert cases[1].findtext("testcode/text") == "assert.strictEqual(soma(1, 2), 3);"
    assert cases[1].findtext("extra/text") == "`soma(1, 2)` deve devolver 3."


def test_so_os_testes_mapeados_vao_para_o_moodle(source: ZimSource) -> None:
    [course], _ = extract_courses(source)
    lesson = next(lesson for _, lesson in course.walk() if lesson.id == "teste-com-expect")
    html_lesson = next(lesson for _, lesson in course.walk() if lesson.id == "pagina-html")

    assert question_xml(lesson, lesson.exercises()[0]).tests == 1
    with pytest.raises(ValueError, match="no automatic grading"):
        question_xml(html_lesson, html_lesson.exercises()[0])
