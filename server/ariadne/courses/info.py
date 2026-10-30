"""O que `read` acrescenta a uma lição: o curso, o módulo, os pré-requisitos e os exercícios."""

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne.db import models


class RequiredLesson(BaseModel):
    document_id: str
    title: str


class ExerciseInfo(BaseModel):
    id: str
    title: str
    language: str
    tests: int
    mapped_tests: int  # os que correm no CodeRunner
    gradable: bool  # tem correção automática no Moodle
    has_solution: bool


class LessonInfo(BaseModel):
    """A lição no seu curso. `requires` são os pré-requisitos (a lição precisa deles) e
    `required_by` as lições que dependem desta."""

    course_id: str
    course_title: str
    course_version: str
    module_id: str
    module_title: str
    position: int
    requires: list[RequiredLesson] = []
    required_by: list[RequiredLesson] = []
    exercises: list[ExerciseInfo] = []
    license_class: str = "open"
    license_name: str = ""
    attribution: str = ""
    moodle_url: str | None = None


def lesson_info(session: Session, document_id: str) -> LessonInfo | None:
    row = session.execute(
        select(models.Lesson, models.Course)
        .join(models.Course, models.Course.id == models.Lesson.course_id)
        .where(models.Lesson.document_id == document_id)
    ).first()
    if row is None:
        return None
    lesson, course = row
    requires = session.execute(
        select(models.Link.dst_document, models.Document.title)
        .join(models.Document, models.Document.id == models.Link.dst_document)
        .where(
            models.Link.src_document == document_id,
            models.Link.kind == "requires",
            models.Link.status != "rejected",
        )
        .order_by(models.Document.title)
    ).all()
    required_by = session.execute(
        select(models.Link.src_document, models.Document.title)
        .join(models.Document, models.Document.id == models.Link.src_document)
        .where(
            models.Link.dst_document == document_id,
            models.Link.kind == "requires",
            models.Link.status != "rejected",
        )
        .order_by(models.Document.title)
    ).all()
    exercises = session.scalars(
        select(models.Exercise)
        .where(models.Exercise.document_id == document_id)
        .order_by(models.Exercise.key)
    ).all()
    return LessonInfo(
        course_id=course.id,
        course_title=course.title,
        course_version=lesson.version,
        module_id=lesson.module_id,
        module_title=lesson.module_title,
        position=lesson.position,
        requires=[RequiredLesson(document_id=d, title=t) for d, t in requires],
        required_by=[RequiredLesson(document_id=d, title=t) for d, t in required_by],
        exercises=[
            ExerciseInfo(
                id=e.key,
                title=e.title,
                language=e.language,
                tests=e.test_count,
                mapped_tests=e.mapped_count,
                gradable=e.gradable,
                has_solution=e.has_solution,
            )
            for e in exercises
        ],
        license_class=course.license_class,
        license_name=course.license_name,
        attribution=course.attribution,
        moodle_url=lesson.platform_url,
    )
