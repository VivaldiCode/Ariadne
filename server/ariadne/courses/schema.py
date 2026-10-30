"""Formato canónico dos cursos (STRUCTURE.md, secção 6.2): o esquema validado.

    curso → módulos → lições → blocos

Os blocos são: texto, exemplo de código, exemplo resolvido, exercício com testes, quiz, solução e
vídeo com transcrição. Este formato é a fonte de verdade; o Moodle é só apresentação. Os modelos
são pydantic com `extra="forbid"`: um ficheiro com um campo desconhecido não carrega (melhor falhar
do que perder informação sem dar por isso).

Referências entre lições (`requires`): «módulo/lição» dentro do mesmo curso ou «curso:módulo/lição»
para outro curso; no curso, `requires` lista os `id` dos cursos pré-requisito.
"""

import hashlib
import json
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ariadne.sources.license import LicenseClass

SCHEMA_VERSION = 1
# Identificadores que entram em caminhos, ids de documento e URL: minúsculas, números, '.', '_'
# e '-'.
SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


class Level(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Identificador (curso, módulo, lição, exercício, teste, quiz, versão).
Slug = Annotated[str, StringConstraints(pattern=SLUG.pattern)]


# --- origem e licença -----------------------------------------------------------------------------


class Origin(Model):
    """De onde veio o curso (rastreabilidade, princípio 7)."""

    source_id: str  # ex.: 'zim:freecodecamp_en_coding-interview-prep'
    source_version: str  # ex.: '2026-08'
    source_path: str | None = None  # onde na fonte
    url: str | None = None


class License(Model):
    name: str  # ex.: 'CC BY-SA 4.0'
    attribution: str  # texto de atribuição que acompanha o curso
    url: str | None = None
    # Obrigatória: a classe de um curso diz-se, nunca se assume (STRUCTURE.md, secção 7).
    license_class: LicenseClass


# --- blocos ---------------------------------------------------------------------------------------


class ExerciseTest(Model):
    """Um teste de um exercício.

    `kind='assert_js'`: `code` é um excerto com `assert.*` (freeCodeCamp) que corre depois do código
    do aluno. `kind='stdio'`: `code` é a chamada ou instrução a executar, `stdin` o que se lhe dá e
    `expected` a saída esperada. `mapped=False` marca os testes que não têm equivalente no
    CodeRunner (com o motivo): ficam no canónico mas não vão para o Moodle."""

    id: Slug
    description: str = ""
    kind: Literal["assert_js", "stdio"] = "assert_js"
    code: str = ""
    stdin: str = ""
    expected: str = ""
    mapped: bool = True
    unmapped_reason: str | None = None

    @model_validator(mode="after")
    def _reason_when_unmapped(self) -> "ExerciseTest":
        if not self.mapped and not self.unmapped_reason:
            raise ValueError(f"test {self.id!r} is unmapped but gives no reason")
        return self


class TextBlock(Model):
    type: Literal["text"] = "text"
    markdown: str


class CodeExampleBlock(Model):
    type: Literal["code_example"] = "code_example"
    language: str
    code: str
    caption: str = ""


class WorkedExampleBlock(Model):
    """Um problema resolvido passo a passo."""

    type: Literal["worked_example"] = "worked_example"
    statement: str
    language: str
    code: str
    explanation: str = ""


class ExerciseBlock(Model):
    """Exercício com testes: o código inicial, o que corre antes/depois do código do aluno (o
    «arnês», escondido) e os testes. A solução fica num `SolutionBlock` à parte."""

    type: Literal["exercise"] = "exercise"
    id: Slug
    title: str = ""
    language: str
    statement: str = ""
    starter_code: str = ""
    harness_before: str = ""
    harness_after: str = ""
    tests: list[ExerciseTest] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_tests(self) -> "ExerciseBlock":
        ids = [test.id for test in self.tests]
        if len(set(ids)) != len(ids):
            raise ValueError(f"exercise {self.id!r} has duplicate test ids")
        return self


class QuizChoice(Model):
    text: str
    correct: bool = False


class QuizQuestion(Model):
    prompt: str
    choices: list[QuizChoice]
    explanation: str = ""

    @model_validator(mode="after")
    def _has_answer(self) -> "QuizQuestion":
        if len(self.choices) < 2:
            raise ValueError("a multiple-choice question needs at least two answers")
        if not any(choice.correct for choice in self.choices):
            raise ValueError("a multiple-choice question needs a correct answer")
        return self


class QuizBlock(Model):
    type: Literal["quiz"] = "quiz"
    id: Slug
    questions: list[QuizQuestion]


class SolutionBlock(Model):
    """Solução de um exercício da mesma lição (pode haver várias alternativas)."""

    type: Literal["solution"] = "solution"
    exercise: Slug
    language: str
    code: str
    explanation: str = ""


class VideoBlock(Model):
    type: Literal["video"] = "video"
    title: str
    url: str | None = None  # referência; o texto serve offline
    transcript: str = ""
    duration_seconds: int | None = None


Block = Annotated[
    TextBlock
    | CodeExampleBlock
    | WorkedExampleBlock
    | ExerciseBlock
    | QuizBlock
    | SolutionBlock
    | VideoBlock,
    Field(discriminator="type"),
]


# --- lições, módulos e curso ----------------------------------------------------------------------


class Lesson(Model):
    id: Slug
    title: str
    summary: str = ""
    objectives: list[str] = Field(default_factory=list)
    level: Level | None = None
    requires: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_path: str | None = None  # onde estava na fonte (rastreabilidade)
    blocks: list[Block] = Field(default_factory=list)

    @model_validator(mode="after")
    def _solutions_point_at_exercises(self) -> "Lesson":
        exercises = {b.id for b in self.blocks if isinstance(b, ExerciseBlock)}
        if len(exercises) != sum(isinstance(b, ExerciseBlock) for b in self.blocks):
            raise ValueError(f"lesson {self.id!r} has exercises with the same id")
        for block in self.blocks:
            if isinstance(block, SolutionBlock) and block.exercise not in exercises:
                raise ValueError(
                    f"lesson {self.id!r}: the solution refers to exercise "
                    f"{block.exercise!r}, which does not exist"
                )
        return self

    def exercises(self) -> list[ExerciseBlock]:
        return [b for b in self.blocks if isinstance(b, ExerciseBlock)]

    def solutions_of(self, exercise_id: str) -> list[SolutionBlock]:
        return [
            b for b in self.blocks if isinstance(b, SolutionBlock) and b.exercise == exercise_id
        ]


class Module(Model):
    id: Slug
    title: str
    summary: str = ""
    lessons: list[Lesson] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_lessons(self) -> "Module":
        ids = [lesson.id for lesson in self.lessons]
        if len(set(ids)) != len(ids):
            raise ValueError(f"module {self.id!r} has lessons with the same id")
        return self


class Course(Model):
    schema_version: int = SCHEMA_VERSION
    id: Slug
    title: str
    version: Slug  # a versão do curso (acompanha a da fonte)
    summary: str = ""
    language: str  # língua do texto (ISO 639-1)
    programming_language: str | None = None  # só nos cursos de programação
    level: Level | None = None
    objectives: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)  # ids de cursos
    origin: Origin
    license: License
    modules: list[Module] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"schema version {value} is not supported (this one is {SCHEMA_VERSION})"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "Course":
        module_ids = [module.id for module in self.modules]
        if len(set(module_ids)) != len(module_ids):
            raise ValueError("there are modules with the same id")
        exercise_ids = [e.id for _, lesson in self.walk() for e in lesson.exercises()]
        if len(set(exercise_ids)) != len(exercise_ids):
            raise ValueError("there are exercises with the same id in the course")
        known = {self.ref(module, lesson) for module, lesson in self.walk()}
        for module, lesson in self.walk():
            for ref in lesson.requires:
                if ":" in ref:  # outra lição de outro curso: não se valida aqui
                    continue
                if ref not in known:
                    raise ValueError(
                        f"lesson {self.ref(module, lesson)!r} requires {ref!r}, "
                        "which does not exist"
                    )
                if ref == self.ref(module, lesson):
                    raise ValueError(f"lesson {ref!r} requires itself")
        cycle = self._cycle()
        if cycle:
            raise ValueError("prerequisite cycle: " + " → ".join(cycle))
        return self

    # --- navegação ---

    @staticmethod
    def ref(module: Module, lesson: Lesson) -> str:
        return f"{module.id}/{lesson.id}"

    def walk(self) -> Iterator[tuple[Module, Lesson]]:
        """Todas as lições pela ordem do curso."""
        for module in self.modules:
            for lesson in module.lessons:
                yield module, lesson

    def lesson(self, ref: str) -> tuple[Module, Lesson]:
        for module, lesson in self.walk():
            if self.ref(module, lesson) == ref:
                return module, lesson
        raise KeyError(ref)

    def _cycle(self) -> list[str] | None:
        edges = {
            self.ref(m, lesson): [r for r in lesson.requires if ":" not in r]
            for m, lesson in self.walk()
        }
        state: dict[str, int] = {}  # 1 = a visitar, 2 = feito

        def visit(node: str, path: list[str]) -> list[str] | None:
            state[node] = 1
            for nxt in edges.get(node, []):
                if state.get(nxt) == 1:
                    return [*path[path.index(nxt) :], nxt] if nxt in path else [node, nxt]
                if nxt not in state and (found := visit(nxt, [*path, nxt])):
                    return found
            state[node] = 2
            return None

        for node in edges:
            if node not in state and (found := visit(node, [node])):
                return found
        return None

    def counts(self) -> dict[str, int]:
        exercises = [e for _, lesson in self.walk() for e in lesson.exercises()]
        tests = [t for e in exercises for t in e.tests]
        return {
            "modules": len(self.modules),
            "lessons": sum(len(m.lessons) for m in self.modules),
            "exercises": len(exercises),
            "tests": len(tests),
            "unmapped_tests": sum(1 for t in tests if not t.mapped),
        }

    def fingerprint(self) -> str:
        """Impressão digital do conteúdo (STRUCTURE.md, 5: «Atualizações»): dois cursos com o mesmo
        conteúdo têm a mesma, qualquer que seja o ficheiro de onde vieram."""
        dumped = self.model_dump(mode="json")
        raw = json.dumps(dumped, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
