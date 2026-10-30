"""Extrator do freeCodeCamp: de um ZIM `freecodecamp_*` para o formato canónico (STRUCTURE.md, 6.2).

O ZIM traz, em `content/curriculum/`, o `index.json` (cursos → módulos → desafios, pela ordem do
currículo) e um `.md` por desafio, no formato do próprio freeCodeCamp:

    ---
    id: ...
    title: ...
    challengeType: 1
    ---
    # --description--      o enunciado (Markdown com HTML inline)
    # --instructions--     o que fazer
    # --hints--            cada teste: uma frase e um bloco ```js com `assert.*`
    # --seed--
    ## --seed-contents--   o código inicial
    ## --before-user-code--  (opcional) corre antes do código do aluno
    ## --after-user-code--   (opcional) corre depois
    # --before-each--      (opcional) corre antes de cada teste
    # --solutions--        a(s) solução(ões)
    # --notes--            notas

Um desafio vira uma lição com um bloco de texto (descrição e instruções), um exercício (código
inicial e testes) e uma solução. Os testes JS do freeCodeCamp passam a testes do CodeRunner (Node)
quando só usam o que o `assert` do chai e os `__helpers` do `chai_shim.js` oferecem; os outros
ficam no canónico como «não mapeados», com o motivo (`ariadne.courses.fcc_tests`).

O freeCodeCamp não declara pré-requisitos: usa-se a ordem do currículo dentro de cada módulo, e a
extração diz-o (`Lesson.requires` encadeia cada lição à anterior do módulo).
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import yaml

from ariadne.courses.fcc_tests import map_test
from ariadne.courses.schema import (
    Course,
    ExerciseBlock,
    ExerciseTest,
    Lesson,
    License,
    Module,
    Origin,
    SolutionBlock,
    TextBlock,
    VideoBlock,
)
from ariadne.sources.license import LicenseClass
from ariadne.sources.zim import ZimSource

CURRICULUM = "content/curriculum/"
INTRO = "content/locales/intro.json"
LICENSE = License(
    license_class=LicenseClass.OPEN,  # o currículo do freeCodeCamp é aberto (CC BY-SA 4.0)
    name="CC BY-SA 4.0",
    url="https://creativecommons.org/licenses/by-sa/4.0/",
    attribution=(
        "Currículo do freeCodeCamp (https://github.com/freeCodeCamp/freeCodeCamp), licença "
        "CC BY-SA 4.0. Pode incluir material da Wikipédia (CC BY-SA 4.0), adaptado."
    ),
)
# challengeType do freeCodeCamp que corre em JavaScript puro (os outros pedem o browser ou um URL).
JS_CHALLENGE = 1
ORDER_CHAIN = "ordem do currículo"

_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_TOP = re.compile(r"^# --([a-z0-9-]+)--[ \t]*$")
_SUB = re.compile(r"^## --([a-z0-9-]+)--[ \t]*$")
_FENCE = re.compile(r"^(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$")
_TAGS = re.compile(r"<[^>]+>")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
JS_LANGUAGES = {"js", "javascript", "jsx"}
PLACEHOLDER_SOLUTION = re.compile(r"solution\s+required", re.IGNORECASE)


class ExtractionError(Exception):
    """O ZIM não tem a estrutura esperada do freeCodeCamp."""


@dataclass
class Fence:
    language: str
    code: str


@dataclass
class Challenge:
    id: str
    title: str
    challenge_type: int
    dashed_name: str
    front: dict[str, Any]
    sections: dict[str, str] = field(default_factory=dict)
    subsections: dict[str, dict[str, str]] = field(default_factory=dict)


def slugify(text: str) -> str:
    slug = _NOT_SLUG.sub("-", text.casefold()).strip("-")
    return slug[:80].strip("-") or "x"


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub("", text)).strip()


def parse_fences(text: str) -> list[tuple[Fence, int, int]]:
    """Os blocos de código de um texto: (bloco, linha onde começa, linha onde acaba)."""
    lines = text.split("\n")
    found: list[tuple[Fence, int, int]] = []
    index = 0
    while index < len(lines):
        match = _FENCE.match(lines[index])
        if not match:
            index += 1
            continue
        marker, language = match.group(1), match.group(2).lower()
        body: list[str] = []
        end = index + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped and set(stripped) == {marker[0]} and len(stripped) >= len(marker):
                break
            body.append(lines[end])
            end += 1
        found.append((Fence(language, "\n".join(body)), index, end))
        index = end + 1
    return found


def parse_challenge(text: str) -> Challenge:
    """Lê um `.md` de desafio do freeCodeCamp. Respeita os blocos de código (um `# --x--` dentro
    de um deles não é um título de secção)."""
    match = _FRONT.match(text.replace("\r\n", "\n"))
    if not match:
        raise ExtractionError("challenge without front matter")
    front = yaml.safe_load(match.group(1)) or {}
    if not isinstance(front, dict) or "title" not in front:
        raise ExtractionError("front matter without a title")
    challenge = Challenge(
        id=str(front.get("id", "")),
        title=str(front["title"]),
        challenge_type=int(front.get("challengeType", 0)),
        dashed_name=str(front.get("dashedName") or slugify(str(front["title"]))),
        front=front,
    )
    top: str | None = None
    sub: str | None = None
    buffer: list[str] = []
    fence_marker: str | None = None

    def flush() -> None:
        body = "\n".join(buffer).strip("\n")
        if top is None:
            return
        if sub is None:
            challenge.sections[top] = body
        else:
            challenge.subsections.setdefault(top, {})[sub] = body

    for line in match.group(2).split("\n"):
        fence = _FENCE.match(line)
        if fence_marker is not None:
            buffer.append(line)
            stripped = line.strip()
            if (
                stripped
                and set(stripped) == {fence_marker[0]}
                and len(stripped) >= len(fence_marker)
            ):
                fence_marker = None
            continue
        if fence:
            fence_marker = fence.group(1)
            buffer.append(line)
            continue
        if heading := _TOP.match(line):
            flush()
            top, sub, buffer = heading.group(1), None, []
        elif (heading := _SUB.match(line)) and top is not None:
            flush()
            sub, buffer = heading.group(1), []
        else:
            buffer.append(line)
    flush()
    return challenge


def parse_hints(body: str) -> list[tuple[str, str]]:
    """(descrição, código) de cada dica/teste de `# --hints--`: um parágrafo e o bloco que segue."""
    lines = body.split("\n")
    hints: list[tuple[str, str]] = []
    cursor = 0
    for fence, start, end in parse_fences(body):
        description = "\n".join(lines[cursor:start]).strip()
        hints.append((description, fence.code))
        cursor = end + 1
    return hints


def _first_code(body: str, *, js_only: bool = False) -> Fence | None:
    fences = [fence for fence, _, _ in parse_fences(body)]
    if js_only:
        fences = [f for f in fences if f.language in JS_LANGUAGES or not f.language]
    return fences[0] if fences else None


def _all_code(body: str) -> list[Fence]:
    return [fence for fence, _, _ in parse_fences(body)]


def language_of(challenge: Challenge) -> str:
    if challenge.challenge_type == JS_CHALLENGE:
        return "javascript"
    seed = challenge.subsections.get("seed", {}).get("seed-contents", "")
    fence = _first_code(seed)
    if fence and fence.language:
        return {"js": "javascript", "py": "python"}.get(fence.language, fence.language)
    return "html" if challenge.challenge_type in (0, 6) else "javascript"


def lesson_from_challenge(
    challenge: Challenge, *, lesson_id: str, exercise_id: str, source_path: str
) -> Lesson:
    sections = challenge.sections
    blocks: list[Any] = []
    if description := sections.get("description", "").strip():
        blocks.append(TextBlock(markdown=description))
    if instructions := sections.get("instructions", "").strip():
        blocks.append(TextBlock(markdown=f"## Instructions\n\n{instructions}"))
    if notes := sections.get("notes", "").strip():
        blocks.append(TextBlock(markdown=f"## Notes\n\n{notes}"))
    if video := challenge.front.get("videoUrl"):
        blocks.append(VideoBlock(title=challenge.title, url=str(video)))

    language = language_of(challenge)
    hints = parse_hints(sections.get("hints", ""))
    if hints or "seed" in sections:
        seed = challenge.subsections.get("seed", {})
        starter = _first_code(seed.get("seed-contents", ""), js_only=language == "javascript")
        tests = [
            map_test(
                test_id=f"t{number}",
                description=strip_tags(description) or f"Teste {number}",
                code=code,
                challenge_type=challenge.challenge_type,
            )
            for number, (description, code) in enumerate(hints, start=1)
        ]
        before = _first_code(seed.get("before-user-code", ""))
        after = _first_code(seed.get("after-user-code", ""))
        each = _first_code(sections.get("before-each", ""))
        harness_after = "\n".join(f.code for f in (after, each) if f is not None)
        blocks.append(
            ExerciseBlock(
                id=exercise_id,
                title=challenge.title,
                language=language,
                starter_code=starter.code if starter else "",
                harness_before=before.code if before else "",
                harness_after=harness_after,
                tests=tests,
            )
        )
        for fence in _all_code(sections.get("solutions", "")):
            if PLACEHOLDER_SOLUTION.search(fence.code):  # o currículo ainda não tem a solução
                continue
            blocks.append(SolutionBlock(exercise=exercise_id, language=language, code=fence.code))
    summary = strip_tags(re.split(r"\n\s*\n", sections.get("description", "").strip())[0])
    return Lesson(
        id=lesson_id,
        title=challenge.title,
        summary=summary[:300],
        source_path=source_path,
        tags=["freecodecamp", language],
        blocks=blocks,
    )


@dataclass
class FccReport:
    """O que a extração fez, para o relatório do `ariadne courses import-fcc`."""

    courses: int = 0
    lessons: int = 0
    exercises: int = 0
    tests: int = 0
    unmapped: Counter[str] = field(default_factory=Counter)
    skipped: list[str] = field(default_factory=list)


def _intro(source: ZimSource) -> dict[str, Any]:
    try:
        _, raw = source.read_bytes(INTRO)
    except Exception:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_index(source: ZimSource) -> dict[str, dict[str, list[dict[str, str]]]]:
    try:
        _, raw = source.read_bytes(CURRICULUM + "index.json")
    except Exception as exc:
        raise ExtractionError(
            "the ZIM has no content/curriculum/index.json (is it not freeCodeCamp?)"
        ) from exc
    try:
        index = json.loads(raw)
    except ValueError as exc:
        raise ExtractionError(f"unreadable index.json: {exc}") from exc
    if not isinstance(index, dict):
        raise ExtractionError("index.json is not a mapping of courses")
    for key, modules in index.items():
        if not isinstance(modules, dict) or not all(isinstance(v, list) for v in modules.values()):
            raise ExtractionError(f"index.json: course {key!r} is not a mapping of modules")
    return index


def course_id_for(key: str, language: str) -> str:
    base = f"freecodecamp-{slugify(key)}"
    return base if language == "en" else f"{base}-{language}"


def extract_courses(
    source: ZimSource, *, chain_requires: bool = True
) -> tuple[list[Course], FccReport]:
    """Todos os cursos do ZIM (normalmente um; o `*_all` traz vários)."""
    index = _read_index(source)
    intro = _intro(source)
    language = source.language or "en"
    report = FccReport()
    courses: list[Course] = []
    for key, modules in index.items():
        course_intro = intro.get(key, {}) if isinstance(intro.get(key), dict) else {}
        blocks_intro = course_intro.get("blocks", {}) if isinstance(course_intro, dict) else {}
        used_lessons: set[str] = set()
        used_exercises: set[str] = set()
        built: list[Module] = []
        for module_key, entries in modules.items():
            module_intro = (
                blocks_intro.get(module_key, {}) if isinstance(blocks_intro, dict) else {}
            )
            lessons: list[Lesson] = []
            for entry in entries:
                path = f"{CURRICULUM}{key}/{module_key}/{entry['slug']}.md"
                try:
                    _, raw = source.read_bytes(path)
                    challenge = parse_challenge(raw.decode("utf-8"))
                except Exception as exc:  # um desafio estragado não derruba o curso
                    report.skipped.append(f"{path}: {exc}")
                    continue
                lesson_id = _unique(slugify(challenge.dashed_name), used_lessons, module_key)
                exercise_id = _unique(f"{lesson_id}-exercise"[:80], used_exercises, module_key)
                lesson = lesson_from_challenge(
                    challenge, lesson_id=lesson_id, exercise_id=exercise_id, source_path=path
                )
                lessons.append(lesson)
            if chain_requires:
                refs = [f"{slugify(module_key)}/{lesson.id}" for lesson in lessons]
                lessons = [
                    lesson.model_copy(update={"requires": [refs[i - 1]] if i else []})
                    for i, lesson in enumerate(lessons)
                ]
            built.append(
                Module(
                    id=slugify(module_key),
                    title=str(module_intro.get("title") or module_key.replace("-", " ").title()),
                    summary=" ".join(strip_tags(p) for p in module_intro.get("intro", [])[:2]),
                    lessons=lessons,
                )
            )
        built = [module for module in built if module.lessons]
        if not built:
            continue
        languages = Counter(
            e.language for module in built for lesson in module.lessons for e in lesson.exercises()
        )
        course = Course(
            id=course_id_for(key, language),
            title=str(course_intro.get("title") or key.replace("-", " ").title()),
            version=source.version,
            summary=" ".join(strip_tags(p) for p in course_intro.get("intro", [])[:2])[:1200],
            language=language,
            programming_language=languages.most_common(1)[0][0] if languages else None,
            origin=Origin(
                source_id=source.id,
                source_version=source.version,
                source_path=f"{CURRICULUM}{key}",
                url=f"https://www.freecodecamp.org/learn/{key}/",
            ),
            license=LICENSE,
            modules=built,
        )
        courses.append(course)
        counts = course.counts()
        report.courses += 1
        report.lessons += counts["lessons"]
        report.exercises += counts["exercises"]
        report.tests += counts["tests"]
        for _, lesson in course.walk():
            for exercise in lesson.exercises():
                for test in exercise.tests:
                    if not test.mapped and test.unmapped_reason:
                        report.unmapped[test.unmapped_reason] += 1
    if not courses:
        raise ExtractionError("the ZIM has no extractable course")
    return courses, report


def _unique(base: str, used: set[str], hint: str) -> str:
    candidate = base
    if candidate in used:
        candidate = f"{base}-{slugify(hint)}"[:80].strip("-")
    number = 2
    while candidate in used:
        candidate = f"{base}-{number}"[:80]
        number += 1
    used.add(candidate)
    return candidate


__all__ = [
    "Challenge",
    "ExerciseTest",
    "ExtractionError",
    "FccReport",
    "course_id_for",
    "extract_courses",
    "lesson_from_challenge",
    "parse_challenge",
    "parse_fences",
    "parse_hints",
    "slugify",
]
