"""Do repositório canónico ao índice, aos documentos e ao grafo (STRUCTURE.md, 6.2 e 6.4).

`sync_courses` percorre o repositório Git e, para cada curso:

- `course_version`: uma linha por versão (contagens, commit, impressão digital); o estado da
  publicação no Moodle não se perde, mas passa a `outdated` se o conteúdo mudou desde que se
  publicou;
- `course`, `lesson` e `exercise`: o índice da versão atual;
- `document`: a lição e a apresentação do curso (`index`) são documentos (`content_type='lesson'`),
  por isso aparecem na pesquisa, em `read` e no grafo;
- `link`: os pré-requisitos (`requires`) entre lições e entre cursos, dirigidos («A requer B»);
- a triagem da curadoria de cada lição (flags) e os pedidos de indexação das que mudaram.

Repetir não altera nada se o repositório não mudou (o commit de cada versão guarda-se).
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne.courses import curation as lesson_curation
from ariadne.courses.coderunner import is_gradable, mapped_tests
from ariadne.courses.repo import CourseRepo
from ariadne.courses.schema import Course, Lesson, Module
from ariadne.courses.source import OVERVIEW, CourseSource, lesson_path, source_id_for
from ariadne.db import models
from ariadne.documents import DocumentInfo, make_document_id, upsert_documents
from ariadne.services import Services
from ariadne.workers.indexer import enqueue_index

logger = logging.getLogger(__name__)

CREATED_BY = "courses"
INDEX_PRIORITY = 1.0  # as lições são conteúdo curado: indexam-se já, sem esperar pelo uso
SEARCH_TEXT_MAX = 200_000


@dataclass
class CourseSyncReport:
    courses: int = 0
    versions: int = 0  # versões vistas de novo (o resto não mudou)
    lessons: int = 0
    exercises: int = 0
    changed_lessons: int = 0
    requires_added: int = 0
    requires_removed: int = 0
    held: int = 0  # lições retidas pela curadoria
    enqueued: int = 0  # pedidos de indexação
    errors: list[str] = field(default_factory=list)


def document_id_of(course_id: str, path: str) -> str:
    return make_document_id(source_id_for(course_id), path)


def _document_meta(module: Module | None, lesson: Lesson | None, position: int) -> dict[str, Any]:
    if module is None or lesson is None:
        return {"course_overview": True}
    return {"module": module.id, "lesson": lesson.id, "position": position}


def sync_courses(
    services: Services, *, force: bool = False, enqueue: bool = True
) -> CourseSyncReport:
    """Atualiza o índice a partir do repositório. `force` refaz tudo, mesmo o que não mudou."""
    report = CourseSyncReport()
    repo = CourseRepo(services.settings.courses_repo)
    if not repo.path.is_dir():
        return report
    head = repo.head()
    if not force and head is not None and head == services.courses_head:
        return report  # o repositório não mudou desde a última ronda sem erros: nada a ler
    with services.sessions() as session:
        services.registry.sync(session)  # as fontes `course:*` entram em `source`
        session.commit()
    for slug in repo.courses():
        try:
            _sync_course(services, repo, slug, report, force=force, enqueue=enqueue)
        except Exception as exc:  # um curso estragado não impede os outros
            logger.exception("Course %s: indexing failed", slug)
            report.errors.append(f"{slug}: {type(exc).__name__}: {exc}")
    # Pré-requisitos entre cursos: só depois de todos terem documentos.
    with services.sessions() as session:
        _course_requires(services, session, repo, report)
        session.commit()
    services.courses_head = head if not report.errors else None
    return report


def _sync_course(
    services: Services,
    repo: CourseRepo,
    slug: str,
    report: CourseSyncReport,
    *,
    force: bool,
    enqueue: bool,
) -> None:
    current = repo.current_version(slug)
    changed_current = force
    loaded: dict[str, Course] = {}
    with services.sessions() as session:
        existing = session.get(models.Course, slug)
        if existing is None or existing.current_version != current:
            changed_current = True
        stale: list[tuple[str, str | None, models.CourseVersion | None]] = []
        for version in repo.versions(slug):
            commit = repo.last_commit(slug, version)
            row = session.get(models.CourseVersion, (slug, version))
            if row is None or row.git_commit != commit or force:
                stale.append((version, commit, row))
                if version == current:
                    changed_current = True
        if stale or changed_current:
            loaded[current] = repo.load(slug, current)
            _upsert_course(session, loaded[current])  # as versões referem o curso
            for version, commit, row in stale:
                course = loaded.get(version) or repo.load(slug, version)
                loaded[version] = course
                _upsert_version(session, course, commit, row)
                report.versions += 1
        session.commit()
    if not changed_current:
        return
    course = loaded.get(current) or repo.load(slug, current)
    source = services.registry.get(source_id_for(slug))
    if not isinstance(source, CourseSource):
        raise RuntimeError(f"the source {source_id_for(slug)} was not registered")
    with services.sessions() as session:
        changed = _index_current(services, session, repo, course, source, report)
        session.commit()
    report.courses += 1
    if enqueue and changed:
        with services.sessions() as session:
            for document_id in changed:
                enqueue_index(session, document_id, INDEX_PRIORITY)
            session.commit()
        report.enqueued += len(changed)


def _upsert_course(session: Session, course: Course) -> None:
    license_ = course.license
    values = {
        "title": course.title,
        "language": course.language,
        "programming_language": course.programming_language,
        "level": course.level.value if course.level else None,
        "summary": course.summary,
        "origin_source_id": course.origin.source_id,
        "license_name": license_.name,
        "license_class": license_.license_class.value,
        "attribution": license_.attribution,
        "current_version": course.version,
        "status": "active",
        "updated_at": func.now(),
    }
    stmt = insert(models.Course).values(id=course.id, **values)
    session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=values))
    session.flush()


def _upsert_version(
    session: Session, course: Course, commit: str | None, row: models.CourseVersion | None
) -> None:
    counts = course.counts()
    fingerprint = course.fingerprint()
    values = {
        "source_version": course.origin.source_version,
        "fingerprint": fingerprint,
        "git_commit": commit,
        "modules": counts["modules"],
        "lessons": counts["lessons"],
        "exercises": counts["exercises"],
        "tests": counts["tests"],
        "unmapped_tests": counts["unmapped_tests"],
    }
    if row is None:
        session.add(models.CourseVersion(course_id=course.id, version=course.version, **values))
        return
    for key, value in values.items():
        setattr(row, key, value)
    if row.publish_status == "published" and row.published_fingerprint != fingerprint:
        row.publish_status = "outdated"  # o canónico mudou desde que se publicou


def _search_text(source: CourseSource, path: str, lesson: Lesson) -> str:
    article = source.read(path)
    return f"{lesson.title}\n{lesson.summary}\n{article.text}"[:SEARCH_TEXT_MAX]


def _index_current(
    services: Services,
    session: Session,
    repo: CourseRepo,
    course: Course,
    source: CourseSource,
    report: CourseSyncReport,
) -> list[str]:
    """Índice da versão atual. Devolve os documentos cujo texto mudou (a indexar)."""
    infos: list[DocumentInfo] = [DocumentInfo(source, OVERVIEW, course.title)]
    metas: dict[str, dict[str, Any]] = {OVERVIEW: _document_meta(None, None, 0)}
    entries: list[tuple[str, Module, Lesson, int]] = []
    for position, (module, lesson) in enumerate(course.walk(), start=1):
        path = lesson_path(module, lesson)
        infos.append(DocumentInfo(source, path, lesson.title))
        metas[path] = _document_meta(module, lesson, position)
        entries.append((path, module, lesson, position))
    upsert_documents(session, infos)
    changed = _store_documents(session, source, course, entries, metas)

    # Uma versão nova não está no Moodle até ser publicada: o que dizia a anterior sai.
    stale = list(
        session.scalars(
            select(models.Lesson.document_id).where(
                models.Lesson.course_id == course.id,
                models.Lesson.version != course.version,
                models.Lesson.platform_url.is_not(None),
            )
        )
    )
    if stale:
        session.execute(
            update(models.Lesson)
            .where(models.Lesson.document_id.in_(stale))
            .values(platform_activity_id=None, platform_kind=None, platform_url=None)
        )
        session.execute(
            update(models.Exercise)
            .where(models.Exercise.document_id.in_(stale))
            .values(platform_question_id=None)
        )
        session.execute(
            text("UPDATE document SET meta = meta - 'moodle_url' WHERE id = ANY(:ids)"),
            {"ids": stale},
        )

    keep = {document_id_of(course.id, path) for path, *_ in entries}
    # Lições que já não estão nesta versão: saem do índice (o documento fica, nunca se apaga).
    gone = list(
        session.scalars(
            select(models.Lesson.document_id).where(
                models.Lesson.course_id == course.id, models.Lesson.document_id.not_in(keep)
            )
        )
    )
    if gone:
        removed = session.execute(
            delete(models.Link).where(
                models.Link.kind == "requires",
                models.Link.created_by == CREATED_BY,
                models.Link.reviewed_by.is_(None),
                or_(models.Link.src_document.in_(gone), models.Link.dst_document.in_(gone)),
            )
        )
        report.requires_removed += int(removed.rowcount or 0)  # type: ignore[attr-defined]
        session.execute(delete(models.Exercise).where(models.Exercise.document_id.in_(gone)))
        session.execute(delete(models.Lesson).where(models.Lesson.document_id.in_(gone)))
        session.execute(
            update(models.Document)
            .where(models.Document.id.in_(gone))
            .values(meta=models.Document.meta.op("||")({"removed_in": course.version}))
        )
    for path, module, lesson, position in entries:
        document_id = document_id_of(course.id, path)
        _store_lesson(session, repo, source, course, module, lesson, position, path, report)
        row = lesson_curation.evaluate_lesson(
            session,
            services.settings,
            document_id=document_id,
            source_id=source.id,
            version=course.version,
            lesson=lesson,
        )
        verdict = lesson_curation.verdict(row.decision, row.reason)
        session.execute(
            update(models.Lesson)
            .where(models.Lesson.document_id == document_id)
            .values(held_reason=verdict.reason)
        )
        report.held += 0 if verdict.publishable else 1
        report.lessons += 1
    _lesson_requires(session, course, report)
    report.changed_lessons += len(changed)
    return sorted(changed)


def _content_hash(source: CourseSource, path: str) -> str:
    article = source.read(path)
    return hashlib.sha256(f"{article.title}\n{article.text}".encode()).hexdigest()


def _store_documents(
    session: Session,
    source: CourseSource,
    course: Course,
    entries: list[tuple[str, Module, Lesson, int]],
    metas: dict[str, dict[str, Any]],
) -> set[str]:
    """Meta dos documentos e deteção do que mudou (hash do texto): o que mudou limpa o «indexado»,
    volta à fila do linker e é devolvido para indexar."""
    changed: set[str] = set()
    paths = [OVERVIEW, *(path for path, *_ in entries)]
    for path in paths:
        document_id = document_id_of(course.id, path)
        digest = _content_hash(source, path)
        previous = session.scalar(
            select(models.Document.meta["content_hash"].astext).where(
                models.Document.id == document_id
            )
        )
        meta = {**metas[path], "content_hash": digest, "course": course.id}
        if previous != digest:
            changed.add(document_id)
            session.execute(
                text(
                    "UPDATE document SET meta = (meta - 'indexed') || CAST(:meta AS jsonb),"
                    " linked_at = NULL, link_lease = NULL, link_attempts = 0 WHERE id = :id"
                ),
                {"meta": _json(meta), "id": document_id},
            )
        else:
            session.execute(
                text("UPDATE document SET meta = meta || CAST(:meta AS jsonb) WHERE id = :id"),
                {"meta": _json(meta), "id": document_id},
            )
    return changed


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _store_lesson(
    session: Session,
    repo: CourseRepo,
    source: CourseSource,
    course: Course,
    module: Module,
    lesson: Lesson,
    position: int,
    path: str,
    report: CourseSyncReport,
) -> None:
    document_id = document_id_of(course.id, path)
    file = _lesson_file(repo, course, module, lesson)
    values = {
        "course_id": course.id,
        "version": course.version,
        "module_id": module.id,
        "module_title": module.title,
        "lesson_id": lesson.id,
        "position": position,
        "title": lesson.title,
        "summary": lesson.summary,
        "requires": list(lesson.requires),
        "search_text": _search_text(source, path, lesson),
        "git_path": file,
        "updated_at": func.now(),
    }
    stmt = insert(models.Lesson).values(document_id=document_id, **values)
    session.execute(stmt.on_conflict_do_update(index_elements=["document_id"], set_=values))
    # Os exercícios refazem-se a cada indexação, mas o que o Moodle já sabe deles não se perde.
    questions = {
        key: question
        for key, question in session.execute(
            select(models.Exercise.key, models.Exercise.platform_question_id).where(
                models.Exercise.document_id == document_id
            )
        )
    }
    session.execute(delete(models.Exercise).where(models.Exercise.document_id == document_id))
    for exercise in lesson.exercises():
        tests = [
            {
                "id": test.id,
                "description": test.description,
                "mapped": test.mapped,
                "unmapped_reason": test.unmapped_reason,
            }
            for test in exercise.tests
        ]
        session.add(
            models.Exercise(
                document_id=document_id,
                key=exercise.id,
                course_id=course.id,
                title=exercise.title or lesson.title,
                language=exercise.language,
                test_count=len(exercise.tests),
                mapped_count=len(mapped_tests(exercise)),
                gradable=is_gradable(exercise),
                has_solution=bool(lesson.solutions_of(exercise.id)),
                tests=tests,
                platform_question_id=questions.get(exercise.id),
            )
        )
        report.exercises += 1


def _lesson_file(repo: CourseRepo, course: Course, module: Module, lesson: Lesson) -> str:
    """O ficheiro da lição no repositório (relativo à raiz)."""
    m_index = [m.id for m in course.modules].index(module.id) + 1
    l_index = [x.id for x in module.lessons].index(lesson.id) + 1
    return f"{course.id}/{course.version}/{m_index:03d}-{module.id}/{l_index:03d}-{lesson.id}.yaml"


# --- grafo: pré-requisitos ------------------------------------------------------------------------


def _desired_edges(course: Course) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for module, lesson in course.walk():
        src = document_id_of(course.id, lesson_path(module, lesson))
        for ref in lesson.requires:
            if ":" in ref:  # outra lição de outro curso
                other, _, path = ref.partition(":")
                edges.add((src, document_id_of(other, path)))
            else:
                edges.add((src, document_id_of(course.id, ref)))
    return edges


def _apply_edges(
    session: Session, scope: set[str], desired: set[tuple[str, str]], report: CourseSyncReport
) -> None:
    """Acrescenta as arestas `requires` que faltam e retira as que o canónico já não diz (só as
    criadas por `courses` e nunca revistas à mão). `scope`: os documentos de origem de que isto
    é o retrato completo."""
    existing_docs = set(
        session.scalars(
            select(models.Document.id).where(
                models.Document.id.in_({d for edge in desired for d in edge} | scope)
            )
        )
    )
    desired = {(a, b) for a, b in desired if a in existing_docs and b in existing_docs}
    if desired:
        stmt = insert(models.Link).values(
            [
                {
                    "src_document": src,
                    "dst_document": dst,
                    "kind": "requires",
                    "score": 1.0,
                    "status": "accepted",
                    "created_by": CREATED_BY,
                }
                for src, dst in sorted(desired)
            ]
        )
        created = session.execute(
            stmt.on_conflict_do_nothing(
                index_elements=["src_document", "dst_document", "kind"]
            ).returning(models.Link.id)
        ).all()
        report.requires_added += len(created)
    stale = session.scalars(
        select(models.Link).where(
            models.Link.kind == "requires",
            models.Link.created_by == CREATED_BY,
            models.Link.reviewed_by.is_(None),
            models.Link.src_document.in_(scope),
        )
    ).all()
    for link in stale:
        if (link.src_document, link.dst_document) not in desired:
            session.delete(link)
            report.requires_removed += 1


def _lesson_requires(session: Session, course: Course, report: CourseSyncReport) -> None:
    scope = {document_id_of(course.id, lesson_path(m, lesson)) for m, lesson in course.walk()}
    _apply_edges(session, scope, _desired_edges(course), report)


def _course_requires(
    services: Services, session: Session, repo: CourseRepo, report: CourseSyncReport
) -> None:
    """Cursos que requerem outros cursos: aresta entre as apresentações («index»)."""
    desired: set[tuple[str, str]] = set()
    scope: set[str] = set()
    graph: dict[str, list[str]] = {}
    for slug in repo.courses():
        try:
            course = repo.load(slug)
        except Exception:
            continue
        scope.add(document_id_of(slug, OVERVIEW))
        graph[slug] = [other for other in course.requires if other != slug]
        if slug in course.requires:
            report.errors.append(f"{slug}: the course requires itself")
    cyclic = _cyclic(graph)
    for members in cyclic:
        report.errors.append("prerequisite cycle between courses: " + " → ".join(members))
    in_cycle = {slug for members in cyclic for slug in members}
    for slug, requires in graph.items():
        for other in requires:
            if slug in in_cycle and other in in_cycle:
                continue  # as arestas de um ciclo não entram no grafo
            desired.add((document_id_of(slug, OVERVIEW), document_id_of(other, OVERVIEW)))
    if scope:
        # As arestas de lições e as de cursos vivem em escopos diferentes: só as `index` aqui.
        _apply_edges(session, scope, desired, report)


def _cyclic(graph: dict[str, list[str]]) -> list[list[str]]:
    """Os ciclos do grafo de cursos (cada um como a lista de cursos, a fechar no primeiro)."""
    found: list[list[str]] = []
    state: dict[str, int] = {}  # 1 = no caminho, 2 = visto

    def visit(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                start = path.index(nxt) if nxt in path else 0
                found.append([*path[start:], nxt])
            elif nxt not in state:
                visit(nxt, [*path, nxt])
        state[node] = 2

    for node in graph:
        if node not in state:
            visit(node, [node])
    return found


def mark_published(
    session: Session,
    course: Course,
    *,
    backend: str,
    platform_course_id: int,
    platform_url: str,
    activities: dict[str, tuple[str, int, str, tuple[int, ...]]],
    report: dict[str, Any],
) -> None:
    """Regista no índice onde o curso ficou na plataforma e que lições lá estão. `activities`:
    «módulo/lição» -> (tipo, id da atividade, URL, ids das perguntas)."""
    now = datetime.now(UTC)
    session.execute(
        update(models.CourseVersion)
        .where(
            models.CourseVersion.course_id == course.id,
            models.CourseVersion.version == course.version,
        )
        .values(
            publish_status="published",
            publish_backend=backend,
            published_fingerprint=course.fingerprint(),
            platform_course_id=platform_course_id,
            platform_url=platform_url,
            published_at=now,
            publish_error=None,
            publish_report=report,
        )
    )
    current = session.scalar(
        select(models.Course.current_version).where(models.Course.id == course.id)
    )
    if current != course.version:
        return  # só a versão atual tem lições no índice
    for ref, (kind, activity_id, url, question_ids) in activities.items():
        document_id = document_id_of(course.id, ref)
        session.execute(
            update(models.Lesson)
            .where(models.Lesson.document_id == document_id)
            .values(platform_activity_id=activity_id, platform_kind=kind, platform_url=url)
        )
        session.execute(
            text(
                "UPDATE document SET meta = meta || jsonb_build_object("
                " 'moodle_url', CAST(:url AS text)) WHERE id = :id"
            ),
            {"url": url, "id": document_id},
        )
        if question_ids:
            session.execute(
                update(models.Exercise)
                .where(models.Exercise.document_id == document_id)
                .values(platform_question_id=question_ids[0])
            )


def mark_failed(session: Session, course_id: str, version: str, error: str) -> None:
    session.execute(
        update(models.CourseVersion)
        .where(models.CourseVersion.course_id == course_id, models.CourseVersion.version == version)
        .values(publish_status="failed", publish_error=error[:2000])
    )


__all__ = [
    "CourseSyncReport",
    "document_id_of",
    "mark_failed",
    "mark_published",
    "sync_courses",
]
