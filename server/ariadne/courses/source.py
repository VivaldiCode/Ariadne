"""`CourseSource`: os cursos canónicos como fonte (`course:<curso>`), lição a lição.

Com isto uma lição é um documento como outro qualquer: o indexador parte-a em chunks, o linker
liga-a aos artigos por título e por vetor, `read` devolve-a e `neighbors` mostra os pré-requisitos.
O caminho de uma lição é «módulo/lição»; o caminho `index` é a apresentação do curso (resumo,
objetivos, lista de lições), onde também se penduram as ligações entre cursos. A solução entra
no texto, numa secção «Solução» à parte, que quem lê pode pedir ou não.
"""

import html as htmllib
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

from ariadne.courses.render import lesson_html, overview_html
from ariadne.courses.repo import CourseRepo
from ariadne.courses.schema import Course, Lesson, Module
from ariadne.sources.base import Article, Link, NotAnArticle, NotFound, Source
from ariadne.sources.zim import extract_blocks, make_sections

OVERVIEW = "index"
_CACHE = 64


def source_id_for(course_id: str) -> str:
    return f"course:{course_id}"


def lesson_path(module: Module, lesson: Lesson) -> str:
    return f"{module.id}/{lesson.id}"


class CourseSource(Source):
    kind = "course"
    content_type = "lesson"

    def __init__(self, repo: CourseRepo, course: Course, *, revision: str = "") -> None:
        counts = course.counts()
        super().__init__(
            id=source_id_for(course.id),
            title=course.title,
            language=course.language,
            version=course.version,
            wing=f"course-{course.id}",
            license_class=course.license.license_class,
            path=str(repo.path / course.id / course.version),
            meta={
                "content_class": "course",
                "archive_only": False,
                "course_id": course.id,
                "course_version": course.version,
                "origin_source_id": course.origin.source_id,
                "license_name": course.license.name,
                "license_class": course.license.license_class.value,
                "attribution": course.license.attribution,
                "git_revision": revision,
                "lessons": counts["lessons"],
                "exercises": counts["exercises"],
            },
        )
        self.course = course
        self._lessons = {lesson_path(m, lesson): (m, lesson) for m, lesson in course.walk()}
        self._cache: OrderedDict[str, Article] = OrderedDict()

    # --- leitura ---

    def read(self, path: str) -> Article:
        cached = self._cache.get(path)
        if cached is not None:
            self._cache.move_to_end(path)
            return cached
        article = self._build(path)
        self._cache[path] = article
        while len(self._cache) > _CACHE:
            self._cache.popitem(last=False)
        return article

    def _build(self, path: str) -> Article:
        if path == OVERVIEW:
            title = self.course.title
            body = overview_html(self.course)
            links = [
                Link(path=ref, title=lesson.title, block=0)
                for ref, (_, lesson) in self._lessons.items()
            ]
        else:
            found = self._lessons.get(path)
            if found is None:
                raise NotFound(path)
            _, lesson = found
            title = lesson.title
            body = lesson_html(lesson, with_solution=True)
            links = [
                Link(path=ref, title=self._lessons[ref][1].title, block=0)
                for ref in lesson.requires
                if ref in self._lessons
            ]
        html = (
            f"<html><head><title>{htmllib.escape(title)}</title></head>"
            f"<body><main><h1>{htmllib.escape(title)}</h1>{body}</main></body></html>"
        )
        blocks, _ = extract_blocks(html, lambda href: None)
        return Article(
            path=path,
            title=title,
            html=html,
            blocks=tuple(blocks),
            sections=tuple(make_sections(title, blocks)),
            links=tuple(links),
        )

    def internal_links(self, path: str) -> list[str]:
        return [link.path for link in self.read(path).links]

    def iter_paths(self) -> Iterator[str]:
        yield OVERVIEW
        yield from self._lessons

    def find_by_title(self, title: str) -> tuple[str, str] | None:
        wanted = title.strip().casefold()
        for ref, (_, lesson) in self._lessons.items():
            if lesson.title.casefold() == wanted:
                return ref, lesson.title
        return None

    def article_count(self) -> int:
        return len(self._lessons) + 1

    def close(self) -> None:
        self._cache.clear()

    # --- o que o índice (`lesson`, `exercise`) precisa ---

    def lesson_of(self, path: str) -> tuple[Module, Lesson]:
        if path == OVERVIEW:
            raise NotAnArticle("the course overview is not a lesson")
        found = self._lessons.get(path)
        if found is None:
            raise NotFound(path)
        return found

    def describe(self) -> dict[str, Any]:
        return {"course": self.course.id, "version": self.course.version}
