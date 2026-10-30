"""Importar cursos para o canónico e publicá-los na plataforma (STRUCTURE.md, 6.2).

ZIM do freeCodeCamp ─▶ `import_fcc` ─▶ Git (formato canónico) ─▶ `sync_courses` ─▶ índice
                                                          └─▶ `publish_course` ─▶ Moodle
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne.config import Settings
from ariadne.courses import curation as lesson_curation
from ariadne.courses import index
from ariadne.courses.backend import CourseBackend, CourseBackendError, CourseBackendUnavailable
from ariadne.courses.fcc import FccReport, extract_courses
from ariadne.courses.moodle import MoodleBackend
from ariadne.courses.repo import CourseRepo, SaveResult, normalize_course
from ariadne.courses.schema import Course
from ariadne.courses.source import lesson_path
from ariadne.courses.verify import ExerciseVerification, apply_verification, verify_course
from ariadne.db import models
from ariadne.licenses import assert_may_send
from ariadne.sandbox import Sandbox
from ariadne.services import Services
from ariadne.sources.zim import ZimSource

logger = logging.getLogger(__name__)


class CoursePublishError(Exception):
    """Não se pôde publicar (curso desconhecido, plataforma recusou, …)."""


class CoursePlatformUnavailable(CoursePublishError):
    """A plataforma não responde ou não está configurada: tenta-se mais tarde."""


def get_backend(settings: Settings) -> CourseBackend:
    return MoodleBackend(
        settings.moodle_url,
        settings.moodle_token,
        public_url=settings.moodle_public_url,
        timeout=settings.moodle_timeout_seconds,
    )


# --- importar -------------------------------------------------------------------------------------


@dataclass
class ImportedCourse:
    course: Course
    saved: SaveResult
    verification: list[ExerciseVerification] = field(default_factory=list)


@dataclass
class ImportResult:
    courses: list[ImportedCourse]
    report: FccReport


def import_fcc(
    services: Services,
    source_id: str,
    *,
    sandbox: Sandbox | None = None,
    chain_requires: bool = True,
) -> ImportResult:
    """Extrai os cursos de um ZIM do freeCodeCamp e guarda cada um no repositório (um commit por
    curso e versão; nenhum se não mudou). Com `sandbox`, a solução oficial de cada exercício corre
    no Jobe e os testes que ela reprova passam a «não mapeados»."""
    source = services.registry.get(source_id)
    if source is None:
        known = ", ".join(s.id for s in services.registry.active()) or "(none)"
        raise CoursePublishError(f"Unknown source: {source_id!r}. Sources: {known}.")
    if not isinstance(source, ZimSource):
        raise CoursePublishError(f"{source_id!r} is not a ZIM.")
    courses, report = extract_courses(source, chain_requires=chain_requires)
    repo = CourseRepo(services.settings.courses_repo)
    imported: list[ImportedCourse] = []
    for course in courses:
        verification: list[ExerciseVerification] = []
        if sandbox is not None:
            verification = verify_course(sandbox, course)
            course = apply_verification(course, verification)
        course = normalize_course(course)
        saved = repo.save(
            course,
            f"Importa {course.title} {course.version} de {source.id}: "
            f"{course.counts()['lessons']} lições, {course.counts()['exercises']} exercícios",
        )
        imported.append(ImportedCourse(course, saved, verification))
    return ImportResult(imported, report)


# --- publicar -------------------------------------------------------------------------------------


@dataclass
class PublishOutcome:
    course_id: str
    version: str
    platform_url: str
    created: int
    updated: int
    unchanged: int
    blocked_by_attempts: int
    held: dict[str, str]
    warnings: list[str]


def publish_course(
    services: Services,
    slug: str,
    version: str | None = None,
    *,
    backend: CourseBackend | None = None,
) -> PublishOutcome:
    """Publica a versão do curso (a atual por omissão) no Moodle. Idempotente. As lições retidas
    pela curadoria não vão. O resultado fica em `course_version` (estado, URL, relatório)."""
    repo = CourseRepo(services.settings.courses_repo)
    try:
        course = repo.load(slug, version)
    except Exception as exc:
        raise CoursePublishError(f"Course {slug!r} cannot be loaded: {exc}") from exc
    index.sync_courses(services, enqueue=False)  # o índice tem de refletir o que se publica
    with services.sessions() as session:  # um curso privado só vai para um Moodle da lista local
        assert_may_send(
            session,
            target="Moodle",
            url=services.settings.moodle_url,
            allowed=services.settings.private_allowed_hosts,
            source_ids=[f"course:{course.id}"],
        )
    own = backend or get_backend(services.settings)
    try:
        with services.sessions() as session:
            current = session.scalar(
                select(models.Course.current_version).where(models.Course.id == slug)
            )
            if current == course.version:
                held = _held(session, course)
            else:  # uma versão antiga também passa pela curadoria
                held = lesson_curation.held_for_version(session, services.settings, course)
        result = own.publish(course, gate=lambda ref: held.get(ref))
    except CourseBackendError as exc:
        with services.sessions() as session:
            index.mark_failed(session, course.id, course.version, str(exc))
            session.commit()
        if isinstance(exc, CourseBackendUnavailable):
            raise CoursePlatformUnavailable(str(exc)) from exc
        raise CoursePublishError(f"Failed to publish {slug}: {exc}") from exc
    finally:
        if backend is None:
            own.close()
    with services.sessions() as session:
        index.mark_published(
            session,
            course,
            backend=own.name,
            platform_course_id=result.course_id,
            platform_url=result.course_url,
            activities={
                ref: (a.kind, a.activity_id, a.url, a.question_ids)
                for ref, a in result.activities.items()
            },
            report=result.summary(),
        )
        session.commit()
    return PublishOutcome(
        course_id=course.id,
        version=course.version,
        platform_url=result.course_url,
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        blocked_by_attempts=result.blocked_by_attempts,
        held=result.held,
        warnings=result.warnings,
    )


def _held(session: Session, course: Course) -> dict[str, str]:
    rows = session.execute(
        select(models.Lesson.module_id, models.Lesson.lesson_id, models.Lesson.held_reason).where(
            models.Lesson.course_id == course.id, models.Lesson.held_reason.is_not(None)
        )
    ).all()
    known = {lesson_path(m, lesson) for m, lesson in course.walk()}
    return {
        f"{module}/{lesson}": reason
        for module, lesson, reason in rows
        if reason is not None and f"{module}/{lesson}" in known
    }
