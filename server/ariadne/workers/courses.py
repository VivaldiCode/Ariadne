"""Worker `courses`: mantém o índice dos cursos em dia e publica no Moodle.

- tarefa periódica: `sync_courses` (o repositório canónico mudou? reindexa, refaz `requires`,
  pede a indexação das lições novas);
- job `publish_course` ({course, version}): publica a versão no Moodle (idempotente). Se o Moodle
  não está configurado ou não responde, o job espera sem gastar tentativas.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ariadne import queue
from ariadne.courses.index import CourseSyncReport, sync_courses
from ariadne.courses.publish import (
    CoursePlatformUnavailable,
    CoursePublishError,
    publish_course,
)
from ariadne.licenses import PrivateSendBlocked
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.services import Services

logger = logging.getLogger(__name__)

KIND = "publish_course"
UNAVAILABLE_RETRY = timedelta(minutes=5)
PUBLISH_PRIORITY = 0.8


def dedupe_key(course: str, version: str | None) -> str:
    return f"{KIND}:{course}:{version or 'current'}"


def enqueue_publish(
    session: Session, course: str, version: str | None = None, priority: float = PUBLISH_PRIORITY
) -> int:
    """Pede a publicação de um curso (a versão atual por omissão). Sem commit."""
    return queue.enqueue(
        session,
        KIND,
        {"course": course, "version": version},
        priority=priority,
        dedupe_key=dedupe_key(course, version),
    )


def run_courses(services: Services, stop: threading.Event | None = None) -> CourseSyncReport:
    report = sync_courses(services)
    if report.courses or report.errors:
        logger.info(
            "Courses: %d reindexed, %d lessons (%d changed), %d errors",
            report.courses,
            report.lessons,
            report.changed_lessons,
            len(report.errors),
        )
    return report


def publish_job(services: Services, job: ClaimedJob, stop: threading.Event | None = None) -> None:
    course = job.payload.get("course")
    version = job.payload.get("version")
    if not isinstance(course, str) or not (version is None or isinstance(version, str)):
        raise Permanent(f"invalid payload: {job.payload!r}")
    settings = services.settings
    if not settings.moodle_url or not settings.moodle_token:
        raise Defer(
            datetime.now(UTC) + UNAVAILABLE_RETRY,
            "Moodle is not configured (MOODLE_URL and MOODLE_TOKEN: `ariadne moodle bootstrap`)",
        )
    try:
        outcome = publish_course(services, course, version)
    except CoursePlatformUnavailable as exc:
        raise Defer(datetime.now(UTC) + UNAVAILABLE_RETRY, str(exc)) from exc
    except PrivateSendBlocked as exc:
        raise Permanent(str(exc)) from exc
    except CoursePublishError as exc:
        raise RuntimeError(str(exc)) from exc
    logger.info(
        "Course %s %s published: %d created, %d updated, %d unchanged, %d held",
        outcome.course_id,
        outcome.version,
        outcome.created,
        outcome.updated,
        outcome.unchanged,
        len(outcome.held),
    )
