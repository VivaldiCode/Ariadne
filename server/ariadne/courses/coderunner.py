"""Exercícios do formato canónico como perguntas CodeRunner (Moodle XML) e como programas do Jobe.

Um exercício JS com testes `assert_js` vira uma pergunta CodeRunner do tipo Node, com um *template
combinador* (um só programa para todos os testes, o que o CodeRunner recomenda): o programa tem o
`chai_shim.js`, o código do aluno, o arnês do exercício e, por teste, uma função assíncrona que
imprime `PASS` ou `FAIL: motivo`; o corretor `EqualityGrader` compara cada resposta com `PASS`.

`render_program` produz exatamente o mesmo programa em Python, para o Jobe correr sem Moodle: é
com ele que `ariadne courses verify` confirma que a solução oficial passa os testes mapeados.
Qualquer mudança no template tem de ser feita nos dois sítios; o teste de Moodle (marca `moodle`)
confirma o do Twig, o do Jobe (marca `jobe`) o do Python.
"""

import json
import re
from dataclasses import dataclass

from ariadne.courses.fcc_tests import shim_source
from ariadne.courses.render import markdown_to_html, text_html_before_exercise
from ariadne.courses.schema import ExerciseBlock, ExerciseTest, Lesson, QuizBlock

SEPARATOR = "#<ab@17943918#@>#"
PASS = "PASS"
NODE_MEMORY_MB = 4096  # memória virtual: o V8 não arranca com menos de ~2-4 GB de espaço
NODE_CPU_SECONDS = 10
LOAD_TIMEOUT_MS = 8000  # o código do aluno carregar (um ciclo infinito à cabeça não prende tudo)
# O programa de fora (de confiança) cria um contexto `vm` novo, carrega o shim, prepara os testes e
# só depois corre o arnês e o código do aluno, que não vê `require`, `process` nem o shim. Quem
# imprime os resultados é o programa de fora: o aluno não consegue forjar um «PASS».
RUNNER = f"""
// Uma promessa rejeitada ou uma exceção solta do código do aluno não pode matar o programa a meio.
process.on("unhandledRejection", () => {{}});
process.on("uncaughtException", () => {{}});
const __describe = (error) => {{
  try {{
    const name = error && error.name ? String(error.name) + ": " : "";
    const text = String(error && error.message !== undefined ? error.message : error);
    return (name + text).slice(0, 300);
  }} catch (e) {{
    return "erro";
  }}
}};
(async () => {{
  const write = (text) => process.stdout.write(text.replace(/\\s+/g, " ").slice(0, 400) + "\\n");
  const ctx = vm.createContext(Object.create(null));
  vm.runInContext(__SHIM, ctx, {{ filename: "shim.js" }});
  const api = vm.runInContext("globalThis.__ariadne", ctx);
  vm.runInContext("delete globalThis.__ariadne", ctx);
  api.prepare(__INPUT.tests);
  let loadError = null;
  for (const part of [__INPUT.before, __INPUT.student, __INPUT.after]) {{
    try {{
      vm.runInContext(part, ctx, {{ timeout: {LOAD_TIMEOUT_MS}, filename: "programa.js" }});
    }} catch (error) {{
      loadError = __describe(error);
      break;
    }}
  }}
  for (let i = 0; i < __INPUT.tests.length; i++) {{
    write(loadError !== null ? "FAIL: " + loadError : String(await api.run(i, __INPUT.student)));
    if (i < __INPUT.tests.length - 1) write("{SEPARATOR}");
  }}
}})();
"""
_TWIG = re.compile(r"\{\{|\{%|\{#")
_ENDVERBATIM = re.compile(r"\{%-?\s*endverbatim")


def mapped_tests(exercise: ExerciseBlock) -> list[ExerciseTest]:
    return [t for t in exercise.tests if t.mapped and t.kind == "assert_js"]


def is_gradable(exercise: ExerciseBlock) -> bool:
    """Tem correção automática no CodeRunner: JavaScript e pelo menos um teste mapeado."""
    return exercise.language == "javascript" and bool(mapped_tests(exercise))


def _preamble() -> str:
    return f"const vm = require('vm');\nconst __SHIM = {json.dumps(shim_source())};\n"


def render_program(
    exercise: ExerciseBlock, student_code: str, tests: list[ExerciseTest] | None = None
) -> str:
    """O programa Node que o Jobe corre: os testes mapeados (ou `tests`) contra `student_code`."""
    chosen = mapped_tests(exercise) if tests is None else tests
    data = {
        "before": exercise.harness_before,
        "student": student_code,
        "after": exercise.harness_after,
        "tests": [test.code for test in chosen],
    }
    return _preamble() + f"const __INPUT = {json.dumps(data)};\n" + RUNNER


def parse_results(stdout: str, count: int) -> list[tuple[bool, str]]:
    """(passou, saída) de cada teste, a partir da saída do programa."""
    parts = [part.strip() for part in stdout.split(SEPARATOR)]
    results = [(part == PASS, part) for part in parts[:count]]
    results += [(False, "no result (the program ended early)")] * (count - len(results))
    return results


def twig_template(exercise: ExerciseBlock) -> str:
    """O template combinador do CodeRunner (Twig). O texto fixo vai em `verbatim`: nada do que lá
    está (shim, arnês) é interpretado pelo Twig. É o mesmo programa de `render_program`."""
    fixed = json.dumps({"before": exercise.harness_before, "after": exercise.harness_after})
    for name, text in (("shim", shim_source()), ("harness", fixed)):
        if _ENDVERBATIM.search(text):
            raise ValueError(
                f"the {name} of exercise {exercise.id!r} closes the Twig verbatim block"
            )
    return (
        "{% verbatim %}\n"
        + _preamble()
        + "{% endverbatim %}\n"
        + "const __INPUT = {\n"
        + "  student: {{ STUDENT_ANSWER | json_encode }},\n"
        + "  tests: [{% for TEST in TESTCASES %}{{ TEST.testcode | json_encode }}, {% endfor %}],\n"
        + "{% verbatim %}\n"
        + f"  ...{fixed},\n"
        + "};\n"
        + RUNNER
        + "{% endverbatim %}\n"
    )


# --- Moodle XML -----------------------------------------------------------------------------------


def cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _text(tag: str, value: str, indent: int = 4, fmt: str | None = None) -> str:
    attribute = f' format="{fmt}"' if fmt else ""
    pad = " " * indent
    return f"{pad}<{tag}{attribute}>\n{pad}  <text>{cdata(value)}</text>\n{pad}</{tag}>\n"


def _raw(tag: str, value: str) -> str:
    return f"    <{tag}>{cdata(value) if value else ''}</{tag}>\n"


@dataclass(frozen=True)
class QuestionXml:
    xml: str
    name: str
    tests: int


# Campos fixos de uma pergunta CodeRunner (tipo Node, template combinador, corretor de igualdade).
FIXED_FIELDS_BEFORE = [
    ("defaultgrade", "1.0000000"), ("penalty", "0.0000000"), ("hidden", "0"), ("idnumber", ""),
    ("coderunnertype", "nodejs"), ("prototypetype", "0"), ("allornothing", "1"),
    ("penaltyregime", "0"), ("precheck", "0"), ("hidecheck", "0"), ("showsource", "0"),
    ("answerboxlines", "18"), ("answerboxcolumns", "100"),
]  # fmt: skip
FIXED_FIELDS_MIDDLE = [
    ("globalextra", ""), ("useace", "1"),
]  # fmt: skip
FIXED_FIELDS_AFTER = [
    ("validateonsave", "0"), ("language", "nodejs"), ("acelang", ""), ("sandbox", ""),
    ("grader", "EqualityGrader"), ("cputimelimitsecs", str(NODE_CPU_SECONDS)),
    ("memlimitmb", str(NODE_MEMORY_MB)), ("templateparams", ""), ("hoisttemplateparams", "0"),
    ("extractcodefromjson", "1"), ("templateparamslang", "twig"), ("templateparamsevalpertry", "0"),
    ("templateparamsevald", ""), ("twigall", "0"), ("uiplugin", "ace"), ("uiparameters", ""),
    ("attachments", "0"), ("attachmentsrequired", "0"), ("maxfilesize", "0"),
    ("filenamesregex", ""), ("filenamesexplain", ""), ("displayfeedback", "1"),
    ("giveupallowed", "0"), ("prototypeextra", ""),
]  # fmt: skip


def _fields(fields: list[tuple[str, str]]) -> str:
    return "".join(f"    <{tag}>{value}</{tag}>\n" for tag, value in fields)


def question_xml(lesson: Lesson, exercise: ExerciseBlock) -> QuestionXml:
    """A pergunta CodeRunner (Node) de um exercício com testes mapeados."""
    if not is_gradable(exercise):
        raise ValueError(f"exercise {exercise.id!r} has no automatic grading")
    tests = mapped_tests(exercise)
    out = ['  <question type="coderunner">\n']
    out.append(_text("name", exercise.title or lesson.title, 4))
    out.append(_text("questiontext", text_html_before_exercise(lesson), 4, "html"))
    out.append(_text("generalfeedback", "", 4, "html"))
    out.append(_fields(FIXED_FIELDS_BEFORE))
    out.append(_raw("answerpreload", exercise.starter_code))
    out.append(_fields(FIXED_FIELDS_MIDDLE))
    out.append(_raw("resultcolumns", '[["Teste", "extra"], ["Resultado", "got"]]'))
    out.append(_raw("template", twig_template(exercise)))
    out.append("    <iscombinatortemplate>1</iscombinatortemplate>\n")
    out.append("    <allowmultiplestdins>0</allowmultiplestdins>\n")
    out.append(_raw("answer", ""))  # a solução oficial fica só no canónico: nunca vai para o Moodle
    out.append(_raw("testsplitterre", "|#<ab@17943918#@>#\\n|ms"))
    out.append(_raw("sandboxparams", '{"sourcefilename": "__tester__.js"}'))
    out.append(_fields(FIXED_FIELDS_AFTER))
    out.append("    <testcases>\n")
    for test in tests:
        out.append(
            '      <testcase testtype="0" useasexample="0" hiderestiffail="0" mark="1.0000000">\n'
            + _text("testcode", test.code, 8)
            + _text("stdin", "", 8)
            + _text("expected", PASS, 8)
            + _text("extra", test.description, 8)
            + _text("display", "SHOW", 8)
            + "      </testcase>\n"
        )
    out.append("    </testcases>\n  </question>\n")
    return QuestionXml("".join(out), exercise.title or lesson.title, len(tests))


def multichoice_xml(block: QuizBlock, lesson: Lesson) -> list[QuestionXml]:
    """As perguntas de escolha de um quiz do canónico como perguntas `multichoice` do Moodle."""
    found: list[QuestionXml] = []
    for number, question in enumerate(block.questions, start=1):
        correct = [c for c in question.choices if c.correct]
        single = len(correct) == 1
        out = ['  <question type="multichoice">\n']
        name = f"{lesson.title} · {number}"
        out.append(_text("name", name, 4))
        out.append(_text("questiontext", markdown_to_html(question.prompt), 4, "html"))
        out.append(_text("generalfeedback", markdown_to_html(question.explanation), 4, "html"))
        out.append("    <defaultgrade>1.0000000</defaultgrade>\n    <penalty>0.3333333</penalty>\n")
        out.append("    <hidden>0</hidden>\n    <idnumber></idnumber>\n")
        out.append(f"    <single>{'true' if single else 'false'}</single>\n")
        out.append("    <shuffleanswers>true</shuffleanswers>\n")
        out.append("    <answernumbering>abc</answernumbering>\n")
        out.append("    <showstandardinstruction>1</showstandardinstruction>\n")
        for tag in ("correctfeedback", "partiallycorrectfeedback", "incorrectfeedback"):
            out.append(_text(tag, "", 4, "html"))
        for choice in question.choices:
            # Várias certas: cada uma vale uma parte e uma errada tira a mesma parte.
            share = 100 / len(correct)
            fraction = (
                (100.0 if single else round(share, 5))
                if choice.correct
                else (0.0 if single else -round(share, 5))
            )
            out.append(f'    <answer fraction="{fraction}" format="html">\n')
            out.append(f"      <text>{cdata(markdown_to_html(choice.text))}</text>\n")
            out.append('      <feedback format="html">\n        <text></text>\n      </feedback>\n')
            out.append("    </answer>\n")
        out.append("  </question>\n")
        found.append(QuestionXml("".join(out), name, len(question.choices)))
    return found


def quiz_xml(questions: list[QuestionXml]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<quiz>\n'
        + "".join(q.xml for q in questions)
        + "</quiz>\n"
    )


def check_twig_safe(text: str) -> bool:
    """O texto não tem delimitadores do Twig (só os blocos `verbatim` podem ter código solto)."""
    return not _TWIG.search(text)
