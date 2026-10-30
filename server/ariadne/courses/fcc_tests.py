"""Do teste do freeCodeCamp ao teste do CodeRunner (Node): o que mapeia e o que não.

Um teste do freeCodeCamp é um excerto JS com `assert.*` do chai que corre no browser depois do
código do aluno (que lhe chega também como `code`, o texto). O CodeRunner corre-o no Node do Jobe
com o `chai_shim.js` (um subconjunto do chai e dos `__helpers`). Mapeia-se quando o excerto só usa
o que o shim oferece; senão fica `mapped=False` com o motivo, e não vai para o Moodle.

Não mapeiam: testes sem nenhum `assert` (não verificariam nada), desafios que não são de JavaScript
puro (HTML/CSS/projetos com `challengeType` ≠ 1), testes que usam o DOM ou a rede do browser,
`expect`/`should` do chai, `require`/`process`/temporizadores (o contexto isolado não os tem),
`__helpers` sem equivalente (`spyOn`, …), métodos do chai fora do subconjunto e módulos ES.
"""

import re
from functools import cache
from importlib import resources

from ariadne.courses.schema import ExerciseTest

# Métodos de `assert` que o shim define (um teste garante que o JS e esta lista não divergem).
SUPPORTED_ASSERT = frozenset(
    [
        "fail",
        "ok",
        "isOk",
        "notOk",
        "isNotOk",
        "equal",
        "notEqual",
        "strictEqual",
        "notStrictEqual",
        "deepEqual",
        "deepStrictEqual",
        "notDeepEqual",
        "isTrue",
        "isNotTrue",
        "isFalse",
        "isNotFalse",
        "isNull",
        "isNotNull",
        "isUndefined",
        "isDefined",
        "exists",
        "notExists",
        "isFunction",
        "isNotFunction",
        "isObject",
        "isNotObject",
        "isArray",
        "isNotArray",
        "isString",
        "isNotString",
        "isNumber",
        "isNotNumber",
        "isBoolean",
        "isNotBoolean",
        "isNaN",
        "isNotNaN",
        "isFinite",
        "typeOf",
        "notTypeOf",
        "instanceOf",
        "notInstanceOf",
        "include",
        "notInclude",
        "includeMembers",
        "sameMembers",
        "sameDeepMembers",
        "property",
        "notProperty",
        "propertyVal",
        "containsAllKeys",
        "hasAllKeys",
        "lengthOf",
        "isEmpty",
        "isNotEmpty",
        "match",
        "notMatch",
        "isAbove",
        "isAtLeast",
        "isBelow",
        "isAtMost",
        "approximately",
        "closeTo",
        "throws",
        "doesNotThrow",
    ]
)
SUPPORTED_HELPERS = frozenset({"removeJSComments", "removeWhiteSpace"})

_ASSERT = re.compile(r"\bassert\.(\w+)")
_HELPER = re.compile(r"\b__helpers\.(\w+)")
_DOM = re.compile(
    r"\b(document|window|navigator|localStorage|sessionStorage|XMLHttpRequest|fetch|"
    r"getComputedStyle|HTMLElement|jQuery|alert|prompt|__pass|__utils)\b|\$\("
)
_CHAI_STYLES = re.compile(r"\bexpect\s*\(|\.should\b")
_REQUIRE = re.compile(r"\brequire\s*\(|\bprocess\.|\bsetTimeout\b|\bsetInterval\b")
_ASSERT_CALL = re.compile(r"\bassert\b")
_ES_MODULES = re.compile(r"^\s*(import|export)\b", re.MULTILINE)
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


@cache
def shim_source() -> str:
    return resources.files("ariadne.courses").joinpath("assets/chai_shim.js").read_text("utf-8")


def unmapped_reason(code: str, challenge_type: int) -> str | None:
    """Porque é que o teste não corre no Node do CodeRunner (`None` = corre)."""
    if challenge_type != 1:
        return (
            f"challenge of type {challenge_type} (HTML, CSS, project or module): "
            "does not run in Node"
        )
    text = _COMMENTS.sub("", code)
    if dom := _DOM.search(text):
        return f"uses the browser ({dom.group(0).rstrip('(')})"
    if _CHAI_STYLES.search(text):
        return "uses chai expect/should (the shim only has assert)"
    if _ES_MODULES.search(text):
        return "uses ES modules (import/export)"
    if _REQUIRE.search(text):
        return "uses require, process or timers, which the isolated context does not have"
    for helper in _HELPER.findall(text):
        if helper not in SUPPORTED_HELPERS:
            return f"uses __helpers.{helper}, with no equivalent in the shim"
    for method in _ASSERT.findall(text):
        if method not in SUPPORTED_ASSERT:
            return f"uses assert.{method}, outside the shim's chai subset"
    if not _ASSERT_CALL.search(text):
        return "no executable assert (checks nothing)"
    return None


def map_test(*, test_id: str, description: str, code: str, challenge_type: int) -> ExerciseTest:
    reason = unmapped_reason(code, challenge_type)
    return ExerciseTest(
        id=test_id,
        description=description,
        kind="assert_js",
        code=code,
        mapped=reason is None,
        unmapped_reason=reason,
    )
