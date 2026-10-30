"""Cursos (módulo «Cursos» da app web): versões, publicação no Moodle, lições e exercícios."""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.courses import index as course_index
from ariadne.courses.repo import CourseRepo, version_key
from ariadne.db import models
from ariadne.documents import LibraryError
from ariadne.workers.courses import enqueue_publish

router = APIRouter(tags=["courses"])


class CourseNotFound(LibraryError):
    pass


class VersionView(BaseModel):
    version: str
    current: bool
    source_version: str
    lessons: int
    exercises: int
    tests: int
    unmapped_tests: int
    git_commit: str | None
    imported_at: datetime
    publish_status: str  # unpublished | published | outdated | failed
    platform_url: str | None
    published_at: datetime | None
    publish_error: str | None
    held: int = 0  # lições retidas pela curadoria (só na versão atual)


class CourseView(BaseModel):
    id: str
    title: str
    language: str
    programming_language: str | None
    level: str | None
    summary: str
    origin_source_id: str | None
    license_name: str
    license_class: str
    attribution: str
    current_version: str
    versions: list[VersionView]


class LessonView(BaseModel):
    document_id: str
    module_id: str
    module_title: str
    lesson_id: str
    position: int
    title: str
    requires: list[str]  # document_ids dos pré-requisitos
    exercises: int
    platform_kind: str | None
    platform_url: str | None
    held_reason: str | None


class CommitView(BaseModel):
    sha: str
    date: str
    subject: str


class CourseDetail(CourseView):
    lessons: list[LessonView]
    commits: list[CommitView]


class TestView(BaseModel):
    __test__ = False

    id: str
    description: str
    mapped: bool
    unmapped_reason: str | None


class ExerciseView(BaseModel):
    document_id: str
    key: str
    title: str
    language: str
    module_title: str
    lesson_title: str
    gradable: bool
    has_solution: bool
    test_count: int
    mapped_count: int
    platform_question_id: int | None
    platform_url: str | None
    tests: list[TestView]


class PublishRequested(BaseModel):
    course: str
    version: str | None
    job_id: int


def _versions(session: Session, course_id: str, current: str) -> list[VersionView]:
    held = {
        row[0]: row[1]
        for row in session.execute(
            select(models.Lesson.course_id, func.count())
            .where(models.Lesson.course_id == course_id, models.Lesson.held_reason.is_not(None))
            .group_by(models.Lesson.course_id)
        )
    }
    rows = session.scalars(
        select(models.CourseVersion).where(models.CourseVersion.course_id == course_id)
    ).all()
    versions = [
        VersionView(
            version=v.version,
            current=v.version == current,
            source_version=v.source_version,
            lessons=v.lessons,
            exercises=v.exercises,
            tests=v.tests,
            unmapped_tests=v.unmapped_tests,
            git_commit=v.git_commit,
            imported_at=v.imported_at,
            publish_status=v.publish_status,
            platform_url=v.platform_url,
            published_at=v.published_at,
            publish_error=v.publish_error,
            held=held.get(course_id, 0) if v.version == current else 0,
        )
        for v in rows
    ]
    return sorted(versions, key=lambda v: version_key(v.version), reverse=True)


def _course_view(session: Session, row: models.Course) -> CourseView:
    return CourseView(
        id=row.id,
        title=row.title,
        language=row.language,
        programming_language=row.programming_language,
        level=row.level,
        summary=row.summary,
        origin_source_id=row.origin_source_id,
        license_name=row.license_name,
        license_class=row.license_class,
        attribution=row.attribution,
        current_version=row.current_version,
        versions=_versions(session, row.id, row.current_version),
    )


@router.get("/courses")
def list_courses(session: SessionDep, principal: PrincipalDep) -> list[CourseView]:
    """The courses in the canonical repository, with their versions and publication status."""
    rows = session.scalars(select(models.Course).order_by(models.Course.title)).all()
    return [_course_view(session, row) for row in rows]


def _get(session: Session, course_id: str) -> models.Course:
    row = session.get(models.Course, course_id)
    if row is None:
        raise CourseNotFound(f"Unknown course: {course_id!r} (see GET /api/courses).")
    return row


@router.get("/courses/{course_id}")
def get_course(
    course_id: str, session: SessionDep, services: ServicesDep, principal: PrincipalDep
) -> CourseDetail:
    """One course: modules and lessons (prerequisites, Moodle) and the Git history."""
    row = _get(session, course_id)
    lessons = session.scalars(
        select(models.Lesson)
        .where(models.Lesson.course_id == course_id)
        .order_by(models.Lesson.position)
    ).all()
    exercise_counts: dict[str, int] = {}
    for key in session.scalars(
        select(models.Exercise.document_id).where(models.Exercise.course_id == course_id)
    ):
        exercise_counts[key] = exercise_counts.get(key, 0) + 1
    required = {
        (src, dst)
        for src, dst in session.execute(
            select(models.Link.src_document, models.Link.dst_document).where(
                models.Link.kind == "requires",
                models.Link.status != "rejected",
                models.Link.src_document.in_([lesson.document_id for lesson in lessons]),
            )
        )
    }
    requires: dict[str, list[str]] = {}
    for src, dst in sorted(required):
        requires.setdefault(src, []).append(dst)
    commits = CourseRepo(services.settings.courses_repo).log(course_id, limit=20)
    view = _course_view(session, row)
    return CourseDetail(
        **view.model_dump(),
        lessons=[
            LessonView(
                document_id=lesson.document_id,
                module_id=lesson.module_id,
                module_title=lesson.module_title,
                lesson_id=lesson.lesson_id,
                position=lesson.position,
                title=lesson.title,
                requires=requires.get(lesson.document_id, []),
                exercises=exercise_counts.get(lesson.document_id, 0),
                platform_kind=lesson.platform_kind,
                platform_url=lesson.platform_url,
                held_reason=lesson.held_reason,
            )
            for lesson in lessons
        ],
        commits=[CommitView(sha=c.sha, date=c.date, subject=c.subject) for c in commits],
    )


@router.get("/courses/{course_id}/exercises")
def list_exercises(
    course_id: str, session: SessionDep, principal: PrincipalDep
) -> list[ExerciseView]:
    """The exercises of the current version and the extracted tests (those that run in CodeRunner
    and those left unmapped, with the reason)."""
    _get(session, course_id)
    rows = session.execute(
        select(models.Exercise, models.Lesson)
        .join(models.Lesson, models.Lesson.document_id == models.Exercise.document_id)
        .where(models.Exercise.course_id == course_id)
        .order_by(models.Lesson.position, models.Exercise.key)
    ).all()
    return [
        ExerciseView(
            document_id=exercise.document_id,
            key=exercise.key,
            title=exercise.title,
            language=exercise.language,
            module_title=lesson.module_title,
            lesson_title=lesson.title,
            gradable=exercise.gradable,
            has_solution=exercise.has_solution,
            test_count=exercise.test_count,
            mapped_count=exercise.mapped_count,
            platform_question_id=exercise.platform_question_id,
            platform_url=lesson.platform_url,
            tests=[TestView(**test) for test in exercise.tests],
        )
        for exercise, lesson in rows
    ]


@router.post("/courses/sync")
def sync_courses(services: ServicesDep, principal: AdminDep) -> dict[str, int]:
    """Re-read the canonical repository and update the index (the `courses` worker does it too)."""
    report = course_index.sync_courses(services)
    return {
        "courses": report.courses,
        "lessons": report.lessons,
        "changed_lessons": report.changed_lessons,
        "held": report.held,
        "errors": len(report.errors),
    }


@router.post("/courses/{course_id}/publish", status_code=202)
def publish(
    course_id: str,
    session: SessionDep,
    principal: AdminDep,
    version: str | None = None,
) -> PublishRequested:
    """Request publication of the version (the current one by default) to Moodle: runs in the
    `courses` worker, idempotent. The status shows in `GET /api/courses`."""
    _get(session, course_id)
    job_id = enqueue_publish(session, course_id, version)
    session.commit()
    return PublishRequested(course=course_id, version=version, job_id=job_id)
