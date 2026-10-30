"""Verificação dos exercícios no Jobe: a solução oficial tem de passar os testes mapeados.

Um teste mapeado que a solução do próprio curso reprova não presta (o shim não o reproduz bem, ou
o teste depende de algo do browser que não vimos): passa a «não mapeado», com o motivo, e nunca
chega ao Moodle. É isto que dá confiança no mapeamento do freeCodeCamp para o CodeRunner.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from ariadne.courses.coderunner import (
    NODE_CPU_SECONDS,
    NODE_MEMORY_MB,
    is_gradable,
    mapped_tests,
    parse_results,
    render_program,
)
from ariadne.courses.schema import Course, ExerciseBlock, ExerciseTest, Lesson, Module
from ariadne.sandbox import Limits, RunResult, Sandbox, SandboxError

logger = logging.getLogger(__name__)
LIMITS = Limits(cpu_seconds=NODE_CPU_SECONDS, memory_mb=NODE_MEMORY_MB)


@dataclass
class ExerciseVerification:
    ref: str  # «módulo/lição»
    exercise: str
    passed: int = 0
    failed: dict[str, str] = field(default_factory=dict)  # id do teste -> porquê
    error: str | None = None  # a solução nem correu (erro de execução, sandbox em baixo)
    skipped: str | None = None  # não há o que verificar (sem solução, sem testes mapeados)
    # O código inicial passa todos os testes: os testes não verificam nada (ou o exercício já vem
    # resolvido), por isso não servem para corrigir.
    vacuous: bool = False
    without_solution: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failed and not self.vacuous


def verify_exercise(
    sandbox: Sandbox, module: Module, lesson: Lesson, exercise: ExerciseBlock
) -> ExerciseVerification:
    ref = f"{module.id}/{lesson.id}"
    result = ExerciseVerification(ref=ref, exercise=exercise.id)
    solutions = lesson.solutions_of(exercise.id)
    tests = mapped_tests(exercise)
    if not is_gradable(exercise):
        result.skipped = "no tests mapped for Node"
        return result
    if solutions:
        run = _run(sandbox, exercise, solutions[0].code, tests)
        if not run.ok:
            result.error = (
                f"{run.outcome.label}: {(run.stderr or run.compile_output).strip()[:300]}"
            )
            return result
        results = parse_results(run.stdout, len(tests))
        for test, (passed, output) in zip(tests, results, strict=True):
            if passed:
                result.passed += 1
            else:
                result.failed[test.id] = output[:300]
    else:
        result.without_solution = True
        result.skipped = "no official solution"
    # O código inicial tem de falhar pelo menos um teste (um erro de execução também é falhar).
    starter = _run(sandbox, exercise, exercise.starter_code, tests)
    if starter.ok:
        starter_results = parse_results(starter.stdout, len(tests))
        result.vacuous = all(passed for passed, _ in starter_results)
    return result


def _run(
    sandbox: Sandbox, exercise: ExerciseBlock, code: str, tests: list[ExerciseTest]
) -> RunResult:
    program = render_program(exercise, code, tests)
    return sandbox.run("nodejs", program, limits=LIMITS, filename="__tester__.js")


def verify_course(
    sandbox: Sandbox, course: Course, progress: Callable[[ExerciseVerification], None] | None = None
) -> list[ExerciseVerification]:
    results: list[ExerciseVerification] = []
    for module, lesson in course.walk():
        for exercise in lesson.exercises():
            try:
                outcome = verify_exercise(sandbox, module, lesson, exercise)
            except SandboxError as exc:
                outcome = ExerciseVerification(
                    ref=f"{module.id}/{lesson.id}", exercise=exercise.id, error=str(exc)
                )
            results.append(outcome)
            if progress:
                progress(outcome)
    return results


def apply_verification(course: Course, results: list[ExerciseVerification]) -> Course:
    """O curso com os testes reprovados pela solução oficial marcados como não mapeados. Se a
    solução nem correu, todos os testes mapeados do exercício ficam por mapear."""
    by_exercise = {r.exercise: r for r in results}
    modules: list[Module] = []
    for module in course.modules:
        lessons: list[Lesson] = []
        for lesson in module.lessons:
            blocks = []
            for block in lesson.blocks:
                if isinstance(block, ExerciseBlock) and block.id in by_exercise:
                    result = by_exercise[block.id]
                    tests = []
                    for test in block.tests:
                        reason = None
                        if test.mapped and result.error is not None:
                            reason = (
                                f"the official solution did not run in Jobe ({result.error[:120]})"
                            )
                        elif test.mapped and result.vacuous:
                            reason = (
                                "the starter code already passes all the tests (they check nothing)"
                            )
                        elif test.mapped and test.id in result.failed:
                            detail = result.failed[test.id][:120]
                            reason = f"the official solution fails it in Jobe ({detail})"
                        if reason:
                            test = test.model_copy(
                                update={"mapped": False, "unmapped_reason": reason}
                            )
                        tests.append(test)
                    block = block.model_copy(update={"tests": tests})
                blocks.append(block)
            lessons.append(lesson.model_copy(update={"blocks": blocks}))
        modules.append(module.model_copy(update={"lessons": lessons}))
    return Course.model_validate(
        course.model_copy(update={"modules": modules}).model_dump(mode="json")
    )
