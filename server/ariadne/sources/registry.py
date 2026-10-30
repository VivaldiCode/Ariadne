"""Catálogo das fontes ZIM da pasta `ZIM_DIR`: abre-as, regista-as na tabela `source`."""

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne.db import models
from ariadne.sources.base import Source, SourceError
from ariadne.sources.classify import ContentClass, effective_class, parse_overrides
from ariadne.sources.license import LicenseClass
from ariadne.sources.zim import ZimSource

if TYPE_CHECKING:
    from ariadne.config import Settings

    pass

logger = logging.getLogger(__name__)

Fingerprint = tuple[tuple[str, int, int], ...]


@dataclass
class SyncReport:
    active: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # estavam registadas e já não há ficheiro
    errors: dict[str, str] = field(default_factory=dict)  # ficheiro -> motivo
    duplicates: dict[str, str] = field(default_factory=dict)  # ficheiro ignorado -> id repetido


def _stat(file: Path) -> os.stat_result | None:
    try:
        return file.stat()
    except OSError:
        return None


class SourceRegistry:
    """Mantém os ZIM abertos e a tabela `source` em dia. Seguro para vários fios."""

    @classmethod
    def from_settings(cls, settings: "Settings") -> "SourceRegistry":
        """O catálogo configurado: pasta de ZIM, repositório dos cursos e classificações manuais."""
        return cls(
            settings.zim_dir,
            courses_repo=settings.courses_repo,
            books_dir=settings.books_dir,
            class_overrides=parse_overrides(settings.source_class_overrides),
        )

    def __init__(
        self,
        zim_dir: Path,
        *,
        courses_repo: Path | None = None,
        books_dir: Path | None = None,
        class_overrides: Mapping[str, ContentClass] | None = None,
    ) -> None:
        self.zim_dir = zim_dir
        self.courses_repo = courses_repo  # o repositório dos cursos canónicos (Git), se houver
        self.books_dir = (
            books_dir  # a biblioteca do Calibre (os livros da tabela `book`), se houver
        )
        self.class_overrides = dict(class_overrides or {})
        self._lock = threading.RLock()
        self._sources: dict[str, Source] = {}
        self._courses: dict[str, tuple[str, Source]] = {}  # curso -> (revisão, fonte)
        self._books: dict[str, tuple[tuple[Any, ...], Source]] = {}  # livro -> (assinatura, fonte)
        self._open: dict[
            str, tuple[tuple[int, int], ZimSource]
        ] = {}  # ficheiro -> (assinatura, fonte)
        self._fingerprint: Fingerprint | None = None

    # --- consulta --------------------------------------------------------------------------

    def get(self, source_id: str) -> Source | None:
        with self._lock:
            return self._sources.get(source_id)

    def active(self) -> list[Source]:
        with self._lock:
            return sorted(self._sources.values(), key=lambda source: source.id)

    # --- sincronização ---------------------------------------------------------------------

    def _scan(self) -> list[Path]:
        return sorted(self.zim_dir.resolve().glob("*.zim")) if self.zim_dir.is_dir() else []

    def _current_fingerprint(
        self, files: list[Path], session: Session | None = None
    ) -> Fingerprint:
        stats = [(f, st) for f in files if (st := _stat(f)) is not None]
        zims = tuple((f.name, st.st_size, st.st_mtime_ns) for f, st in stats)
        parts: Fingerprint = zims
        if self.courses_repo is not None:
            # Os cursos mudam com cada commit: a revisão do repositório entra na assinatura.
            from ariadne.courses.repo import CourseRepo

            head = CourseRepo(self.courses_repo).head() or ""
            parts = (*parts, (f"courses:{head}", 0, 0))
        if session is not None:
            # As marcas «privada» à mão (`source.license_override`) também mudam a assinatura.
            marks, digest = session.execute(
                select(
                    func.count(),
                    func.coalesce(func.md5(func.string_agg(models.Source.id, ",")), ""),
                ).where(models.Source.license_override.is_not(None))
            ).one()
            parts = (*parts, (f"marks:{marks}:{digest}", 0, 0))
        if session is not None and self.books_dir is not None:
            # Os livros mudam na tabela `book` (a CLI, a app): o número e a última alteração.
            count, last = session.execute(
                select(func.count(), func.max(models.Book.updated_at)).where(
                    models.Book.status == "active"
                )
            ).one()
            parts = (*parts, (f"books:{count}:{last.isoformat() if last else ''}", 0, 0))
        return parts

    def sync_if_changed(self, session: Session) -> SyncReport | None:
        """Só toca na base de dados se a pasta mudou desde a última sincronização."""
        with self._lock:
            files = self._scan()
            if self._fingerprint is not None and self._current_fingerprint(files, session) == (
                self._fingerprint
            ):
                return None
            return self.sync(session)

    def sync(self, session: Session) -> SyncReport:
        """Abre os ZIM da pasta, faz upsert em `source` e marca como 'missing' os que sumiram."""
        with self._lock:
            report = SyncReport()
            fingerprint = self._refresh(report, session)
            try:
                report.missing = self._write(session, self._sources)
            except Exception:
                # Sem a tabela em dia, a pasta não conta como sincronizada: o próximo pedido repete.
                session.rollback()
                self._fingerprint = None
                raise
            self._fingerprint = fingerprint
            return report

    def load(self) -> SyncReport:
        """Só abre os ZIM da pasta, sem tocar na base de dados (para correr noutra máquina, ex.:
        `ariadne vectors build --out`). A tabela `source` fica por escrever."""
        with self._lock:
            report = SyncReport()
            self._refresh(report)
            self._fingerprint = None  # um `sync_if_changed` posterior ainda tem de escrever
            return report

    def _refresh(self, report: SyncReport, session: Session | None = None) -> Fingerprint:
        """Abre os ZIM novos ou alterados e fecha os que sobraram; devolve a assinatura da pasta.
        Com `session` carrega também os livros (tabela `book`)."""
        files = self._scan()
        current: dict[str, tuple[tuple[int, int], ZimSource]] = {}
        fingerprint = self._current_fingerprint(files, session)
        for file in files:
            stat = _stat(file)
            if stat is None:  # symlink partido, ficheiro que desapareceu a meio
                report.errors[file.name] = "file not accessible"
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            cached = self._open.get(str(file))
            if cached is not None and cached[0] == signature:
                current[str(file)] = cached
                continue
            try:
                current[str(file)] = (signature, ZimSource.open(file))
            except (SourceError, OSError) as exc:
                logger.warning("ZIM ignored: %s", exc)
                report.errors[file.name] = str(exc)

        winners: dict[str, Source] = {}
        for _, zim in current.values():
            self._apply_overrides(zim)
        for _, zim in current.values():
            other = winners.get(zim.id)
            if other is not None:  # dois ficheiros com o mesmo Name: fica o mais recente
                loser, newer = sorted([other, zim], key=lambda s: (s.version, s.path or ""))
                report.duplicates[Path(loser.path or "").name] = newer.id
                logger.warning("Duplicate ZIM %s: keeping %s", newer.id, newer.path)
                winners[zim.id] = newer
            else:
                winners[zim.id] = zim

        for course_source in self._load_courses(report):
            winners[course_source.id] = course_source
        for book_source in self._load_books(report, session):
            winners[book_source.id] = book_source

        kept = {id(source) for source in winners.values()}
        for _, source in [*self._open.values(), *current.values()]:
            if id(source) not in kept:
                source.close()
        self._open = {k: v for k, v in current.items() if id(v[1]) in kept}
        self._sources = winners
        report.active = sorted(winners)
        return fingerprint

    def _apply_overrides(self, zim: ZimSource) -> None:
        """`SOURCE_CLASS_OVERRIDES`: a classificação manual manda sobre a dos metadados."""
        default = ContentClass(str(zim.meta.get("content_class", "article")))
        chosen = effective_class(zim.id, default, self.class_overrides)
        zim.class_overrides = self.class_overrides  # as correções por secção (prefixo de caminho)
        if chosen is not default:
            zim.meta["content_class"] = chosen.value
            zim.meta["class_reason"] = "SOURCE_CLASS_OVERRIDES"
            zim.meta["archive_only"] = chosen is ContentClass.COURSE and bool(
                zim.meta.get("extractable")
            )

    def _load_courses(self, report: SyncReport) -> list[Source]:
        """A versão atual de cada curso do repositório canónico, como fonte (`course:<id>`)."""
        if self.courses_repo is None or not self.courses_repo.is_dir():
            return []
        from ariadne.courses.repo import CourseRepo, CourseRepoError
        from ariadne.courses.source import CourseSource

        repo = CourseRepo(self.courses_repo)
        head = repo.head() or ""
        found: list[Source] = []
        keep: dict[str, tuple[str, Source]] = {}
        for slug in repo.courses():
            cached = self._courses.get(slug)
            if cached is not None and cached[0] == head:
                keep[slug] = cached
                found.append(cached[1])
                continue
            try:
                course = repo.load(slug)
            except CourseRepoError as exc:
                logger.warning("Course ignored: %s (%s)", slug, exc)
                report.errors[f"course:{slug}"] = str(exc)
                continue
            source = CourseSource(repo, course, revision=head)
            keep[slug] = (head, source)
            found.append(source)
        self._courses = keep
        return found

    def _load_books(self, report: SyncReport, session: Session | None) -> list[Source]:
        """Os livros ativos da tabela `book`, como fontes (`book:<id>`). Um livro cujo ficheiro não
        está visível em `BOOKS_DIR` fica de fora (e marcado `missing`). Sem base de dados
        (`load()`) não há livros."""
        if session is None or self.books_dir is None:
            return []
        from ariadne.books.source import BookData, BookSource

        found: list[Source] = []
        keep: dict[str, tuple[tuple[Any, ...], Source]] = {}
        rows = session.scalars(
            select(models.Book).where(models.Book.status == "active").order_by(models.Book.id)
        ).all()
        for row in rows:
            file = self.books_dir / row.file_path
            if not file.is_file():
                report.errors[f"book:{row.id}"] = f"file missing: {row.file_path}"
                continue
            signature = (row.file_sha256, row.updated_at, row.chapters, row.license_class)
            cached = self._books.get(row.id)
            if cached is not None and cached[0] == signature:
                keep[row.id] = cached
                found.append(cached[1])
                continue
            source = BookSource(
                BookData(
                    id=row.id,
                    title=row.title,
                    file=file,
                    format=row.format,
                    file_sha256=row.file_sha256,
                    license_class=LicenseClass(row.license_class),
                    license_name=row.license_name,
                    attribution=row.attribution,
                    authors=tuple(row.authors),
                    language=row.language,
                    chapters=row.chapters,
                    pages=row.pages,
                )
            )
            keep[row.id] = (signature, source)
            found.append(source)
        for slug, (_, old) in self._books.items():
            if slug not in keep:
                old.close()
        self._books = keep
        return found

    def _write(self, session: Session, sources: Mapping[str, Source]) -> list[str]:
        # A classe mais restritiva ganha sempre: uma fonte marcada privada à mão (ZIM incluído)
        # nunca volta a aberta por a sincronização reescrever o que o objeto declara.
        marked = set(
            session.scalars(
                select(models.Source.id).where(models.Source.license_override == "private")
            )
        )
        for source in sources.values():
            source.license_class = (
                LicenseClass.PRIVATE if source.id in marked else source.declared_license_class
            )
        before = {
            source_id: license_class
            for source_id, license_class in session.execute(
                select(models.Source.id, models.Source.license_class).where(
                    models.Source.id.in_(list(sources))
                )
            )
        }
        for source in sources.values():
            values = {
                "id": source.id,
                "kind": source.kind,
                "title": source.title,
                "language": source.language,
                "version": source.version,
                "path": source.path,
                "wing": source.wing,
                "status": "active",
                "license_class": source.license_class.value,
                "meta": source.meta,
            }
            stmt = insert(models.Source).values(**values)
            update_values: dict[str, Any] = {k: v for k, v in values.items() if k != "id"}
            # O que o importador do Wiki.js aprendeu da fonte (`meta.boilerplate`) não vem do
            # objeto da fonte: sobrevive à sincronização.
            kept = func.jsonb_strip_nulls(
                func.jsonb_build_object("boilerplate", models.Source.meta["boilerplate"])
            )
            update_values["meta"] = stmt.excluded.meta.op("||")(kept)
            session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=update_values))
            if before.get(source.id) == "private" and source.license_class.value == "open":
                # Uma fonte privada que passa a aberta (decisão explícita, p. ex. o curso mudou
                # de classe no canónico): os documentos acompanham-na. O contrário já o faz o
                # Postgres (`trg_source_license`).
                session.execute(
                    update(models.Document)
                    .where(models.Document.source_id == source.id)
                    .values(license_class="open")
                )
        missing: list[str] = []
        for kind in (ZimSource.kind, "course", "book"):
            gone = (
                update(models.Source)
                .where(models.Source.kind == kind)
                .where(models.Source.status.not_in(("missing", "archived")))
                .where(models.Source.id.not_in(list(sources)))
                .values(status="missing")
                .returning(models.Source.id)
            )
            missing += session.scalars(gone)
        session.commit()
        return sorted(missing)

    def close(self) -> None:
        with self._lock:
            for _, source in self._open.values():
                source.close()
            self._open.clear()
            self._sources.clear()
            self._courses.clear()
            self._books.clear()
            self._fingerprint = None
