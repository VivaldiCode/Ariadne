"""O conteúdo de uma release: só o que a STRUCTURE.md, secção 16, deixa sair.

**Entra:** o conteúdo curado da classe aberto (as páginas que o Ariadne publicou no Wiki.js e as
**versões abertas** dos cursos canónicos), o registo de fontes (só campos seguros) e as
configurações numa lista branca (números, interruptores e uns poucos nomes de modelo: nunca URL,
e-mails, caminhos nem segredos). **Nunca entra:** dados de utilizadores (principais, chaves,
sessões, `query_log`, `query_result`, `tool_call`: este módulo nem lê essas tabelas) e nada da
classe privado ou derivado dele. **Fica de fora** o que se descarrega de novo: os ZIM originais e
os pesos de modelos.

Como se garante: tudo o que se escolhe passa por `document_is_open()` / pela classe da fonte e,
**curso a curso e versão a versão**, pela licença do próprio ficheiro canónico; e, no fim, uma
verificação final sobre **o que foi mesmo escrito** (a licença lida dos bytes do `course.yaml`
enviado, as fontes e os documentos por `licenses.assert_exportable`, as configurações contra a
lista branca): se algo privado passar, a release recusa-se, não é escrita e fica em
`license_block` (só contagens e uma amostra de ids, nunca texto).

O conteúdo vai para um `Sink` (o `tar.gz` em stream que alimenta o `age`): não há árvore em claro
no disco. As páginas ficam em memória até ao fim, porque o SQL/CSV de restauro as leva também.
As páginas exportam-se do que o Ariadne publicou (tabela `curation`), não de uma leitura ao vivo
do Wiki.js: as edições à mão no Wiki.js estão no backup local (pg_dump da base `wikijs`).
"""

import json
import os
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ariadne import __version__, licenses
from ariadne.config import Settings
from ariadne.courses.repo import CourseRepo, CourseRepoError
from ariadne.curation import pipeline
from ariadne.db import models
from ariadne.documents import make_document_id
from ariadne.release.sink import Sink
from ariadne.services import Services
from ariadne.sources.license import LicenseClass
from ariadne.wiki import render

FORMAT = "ariadne-release/1"
SCHEMA = "ariadne_release"
MAX_COURSE_FILE = 64 * 1024 * 1024
_SAFE_SEGMENT = re.compile(r"[\w.\-]+", re.UNICODE)

# Configurações: números e interruptores (nunca segredos) e só estes textos.
_SETTING_STRINGS = (
    "embedder",
    "embedder_model",
    "embedder_revision",
    "embedder_variant",
    "semantic_store",
    "wikijs_locale",
    "libretranslate_pt_code",
)
# Do `meta` das fontes só estes campos saem (nada de caminhos, ids de ficheiro, revisões…).
_META_KEYS = (
    "zim_name", "zim_language", "uuid", "flavour", "date", "creator", "description", "kiwix_book",
    "content_class", "archive_only", "extractable", "project", "wiki_language", "site", "articles",
    "course_id", "course_version", "license_name", "license_class", "attribution", "lessons",
    "exercises", "book_id", "book_format", "authors", "chapters", "pages",
)  # fmt: skip


@dataclass
class ContentReport:
    sources: int = 0
    wiki_pages: int = 0
    courses: int = 0
    course_versions: int = 0
    course_files: int = 0
    settings_keys: int = 0
    files: int = 0
    bytes: int = 0
    excluded: dict[str, int] = field(default_factory=dict)  # só contagens (ficam locais)
    skipped: dict[str, int] = field(default_factory=dict)
    # Para a verificação final e para o ensaio (nunca vão para o manifesto):
    source_ids: list[str] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    shipped_courses: dict[tuple[str, str], str] = field(
        default_factory=dict
    )  # licença lida do ficheiro enviado

    def counts(self) -> dict[str, int]:
        return {
            "sources": self.sources,
            "wiki_pages": self.wiki_pages,
            "courses": self.courses,
            "course_files": self.course_files,
        }


# --- configurações ----------------------------------------------------------------------------


def public_settings(settings: Settings) -> dict[str, Any]:
    """Lista branca: todos os campos numéricos e booleanos e uns poucos nomes de modelo. Nenhum
    URL, e-mail, caminho, host nem segredo (nem pelo nome, nem pelo valor) chega aqui."""
    data = settings.model_dump(mode="json")
    out: dict[str, Any] = {}
    for name, value in sorted(data.items()):
        if isinstance(value, bool | int | float) or name in _SETTING_STRINGS:
            if isinstance(value, str) and ("://" in value or "@" in value):
                continue
            out[name] = value
    return out


def settings_are_safe(config: dict[str, Any]) -> bool:
    """A verificação final das configurações que vão no pacote."""
    for name, value in config.items():
        if not (isinstance(value, bool | int | float) or name in _SETTING_STRINGS):
            return False
        if isinstance(value, str) and ("://" in value or "@" in value):
            return False
    return True


# --- csv (os dados do restauro; nunca SQL do arquivo) ------------------------------------------


def _csv_field(value: Any) -> str:
    if value is None:
        return r"\N"
    if isinstance(value, dict | list):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return '"' + str(value).replace("\x00", "").replace('"', '""') + '"'


def csv_bytes(rows: list[list[Any]]) -> bytes:
    """CSV para `COPY … WITH (FORMAT csv, NULL '\\N')`: todo o texto entre aspas (por isso um
    valor «\\N» ou vazio nunca se confunde com NULL)."""
    return "".join(",".join(_csv_field(v) for v in row) + "\n" for row in rows).encode("utf-8")


def schema_sql() -> str:
    return resources.files("ariadne.release").joinpath("schema.sql").read_text(encoding="utf-8")


# --- ficheiros --------------------------------------------------------------------------------


def _put(sink: Sink, report: ContentReport, relative: str, data: str | bytes) -> None:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    sink.add_bytes(relative, raw)
    report.files += 1
    report.bytes += len(raw)


def _safe_wiki_path(value: str) -> str | None:
    """O caminho de uma página como ficheiro: segmentos «normais», sem `..` nem `/` inicial."""
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        return None
    normal = posixpath.normpath(value)
    if normal != value or normal.startswith(".."):
        return None
    parts = normal.split("/")
    if not all(_SAFE_SEGMENT.fullmatch(part) and part not in (".", "..") for part in parts):
        return None
    return normal


# --- páginas ----------------------------------------------------------------------------------


def _page_rows(session: Session) -> list[tuple[models.Document, models.Curation, models.Source]]:
    return [
        (document, row, source)
        for document, row, source in session.execute(
            select(models.Document, models.Curation, models.Source)
            .join(models.Curation, models.Curation.document_id == models.Document.id)
            .join(models.Source, models.Source.id == models.Document.source_id)
            .where(
                models.Document.wiki_page_id.is_not(None),
                models.Curation.decision == pipeline.PUBLISHED,
                models.Curation.published_md.is_not(None),
                licenses.document_is_open(models.Document),
            )
            .order_by(models.Document.id)
        )
    ]


def _open_wiki_paths(session: Session, source_id: str, wanted: set[str]) -> dict[str, str]:
    """Links internos do corpo: o caminho da página de cada destino ABERTO e já importado."""
    ids = [make_document_id(source_id, p) for p in wanted]
    if not ids:
        return {}
    found = {
        doc_id: wiki_path
        for doc_id, wiki_path in session.execute(
            select(models.Document.id, models.Document.meta["wiki_path"].astext).where(
                models.Document.id.in_(ids),
                models.Document.wiki_page_id.is_not(None),
                licenses.document_is_open(models.Document),
            )
        )
        if wiki_path
    }
    return {
        p: found[make_document_id(source_id, p)]
        for p in wanted
        if make_document_id(source_id, p) in found
    }


def _token_lookup(targets: dict[str, str]) -> Callable[[str], str | None]:
    def lookup(path: str) -> str | None:
        wiki_path = targets.get(path)
        return f"/{wiki_path}" if wiki_path else None

    return lookup


def _export_pages(
    session: Session, sink: Sink, report: ContentReport
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    index: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    taken: set[str] = set()
    for document, row, source in _page_rows(session):
        wiki_path = _safe_wiki_path(str(document.meta.get("wiki_path") or ""))
        if wiki_path is None or wiki_path in taken:
            report.skipped["pagina_sem_caminho_seguro"] = (
                report.skipped.get("pagina_sem_caminho_seguro", 0) + 1
            )
            continue
        taken.add(wiki_path)
        body_md = row.published_md or ""
        targets = _open_wiki_paths(session, document.source_id, pipeline.token_paths(body_md))
        body = pipeline.resolve_tokens(body_md, _token_lookup(targets))
        zim_name = str(source.meta.get("zim_name") or "")
        page = render.assemble(
            render.header(
                source_title=source.title,
                source_id=document.source_id,
                version=source.version,
                path=document.path,
                kiwix_url=None,
                curation="publicado pelo Ariadne (curadoria)",
                archived=row.archived_at is not None,
            ),
            body,
            "",
            render.footer(
                render.license_for(document.source_id, zim_name, document.language, document.title),
                source.title,
            ),
        )
        _put(sink, report, f"wiki/{wiki_path}.md", page)
        report.document_ids.append(document.id)
        index.append(
            {
                "document_id": document.id,
                "path": wiki_path,
                "title": document.title,
                "language": document.language,
                "source_id": document.source_id,
                "source_version": source.version,
                "fingerprint": row.fingerprint,
            }
        )
        pages.append({**index[-1], "body": page})
    report.wiki_pages = len(index)
    return index, pages


# --- cursos -----------------------------------------------------------------------------------


def _license_of_shipped(yaml_bytes: bytes) -> str:
    """A classe de licença lida do `course.yaml` que vai mesmo no pacote."""
    try:
        data = yaml.safe_load(yaml_bytes)
        return str(data["license"]["license_class"])
    except (yaml.YAMLError, KeyError, TypeError):
        return "unknown"


def _export_courses(
    session: Session, repo_path: Path | None, sink: Sink, report: ContentReport
) -> list[dict[str, Any]]:
    if repo_path is None or not repo_path.is_dir():
        return []
    repo = CourseRepo(repo_path)
    listed: list[dict[str, Any]] = []
    private_courses = 0
    private_versions = 0
    for slug in repo.courses():
        source_id = f"course:{slug}"
        row = session.get(models.Source, source_id)
        if row is not None and (row.license_class == "private" or row.license_override):
            private_courses += 1  # a base manda: a marca à mão vale para todas as versões
            continue
        exported = 0
        slug_private = 0
        for version in repo.versions(slug):
            try:
                course = repo.load(slug, version)  # cada versão tem a sua licença
            except CourseRepoError:
                report.skipped["curso_ilegivel"] = report.skipped.get("curso_ilegivel", 0) + 1
                continue
            if course.license.license_class is LicenseClass.PRIVATE:
                private_versions += 1
                slug_private += 1
                continue
            base = repo.path / slug / version
            files = 0
            for current, folders, names in os.walk(base, followlinks=False):
                folders[:] = [
                    f for f in folders if f != ".git" and not (Path(current) / f).is_symlink()
                ]
                for name in sorted(names):
                    file = Path(current) / name
                    if file.is_symlink() or not file.is_file():
                        continue
                    if file.stat().st_size > MAX_COURSE_FILE:
                        continue
                    relative = f"courses/{slug}/{version}/{file.relative_to(base).as_posix()}"
                    data = file.read_bytes()
                    if relative.endswith(f"/{version}/course.yaml"):
                        report.shipped_courses[(slug, version)] = _license_of_shipped(data)
                    _put(sink, report, relative, data)
                    files += 1
            report.course_files += files
            report.course_versions += 1
            exported += 1
            listed.append({"slug": slug, "version": version, "source_id": source_id})
        if exported:
            report.courses += 1
            report.source_ids.append(source_id)
        elif slug_private:
            private_courses += 1  # nenhuma versão é aberta: o curso inteiro fica de fora
    if private_courses:
        report.excluded["cursos_privados"] = private_courses
    if private_versions:
        report.excluded["versoes_privadas_de_cursos"] = private_versions
    return listed


# --- registo de fontes ------------------------------------------------------------------------


def _safe_meta(meta: dict[str, Any], exported: set[str]) -> dict[str, Any]:
    out = {k: meta[k] for k in _META_KEYS if k in meta}
    origin = meta.get("origin_source_id")
    if isinstance(origin, str) and origin in exported:  # só se a origem também é aberta e sai
        out["origin_source_id"] = origin
    return out


# --- verificação final ------------------------------------------------------------------------


def final_check(session: Session, report: ContentReport, config: dict[str, Any]) -> None:
    """A última barreira, sobre o que foi mesmo escrito: levanta `LicenseBlocked` se algo privado
    (ou de licença desconhecida) vai no pacote, ou se as configurações saem da lista branca."""
    bad = [
        licenses.Blocked("course", f"{slug}/{version}", f"license {klass} in the shipped file")
        for (slug, version), klass in report.shipped_courses.items()
        if klass != LicenseClass.OPEN.value
    ]
    if bad:
        raise licenses.LicenseBlocked("release externa", bad)
    if not settings_are_safe(config):
        raise licenses.LicenseBlocked(
            "release externa",
            [licenses.Blocked("config", "settings.json", "outside the allowlist")],
        )
    licenses.assert_exportable(
        session,
        output="release externa",
        source_ids=report.source_ids,
        document_ids=report.document_ids,
    )


# --- orquestração -----------------------------------------------------------------------------


def build_content(
    services: Services, sink: Sink, *, canary: str, run_id: int | None, stamp: str
) -> ContentReport:
    """Escreve o conteúdo da release no `sink` e devolve o relatório. Levanta `LicenseBlocked`
    (e a release não se escreve) se a verificação final apanhar algo privado."""
    settings = services.settings
    report = ContentReport()
    with services.sessions() as session:
        sources = list(
            session.scalars(
                select(models.Source)
                .where(
                    models.Source.license_class == LicenseClass.OPEN.value,
                    models.Source.license_override.is_(None),
                )
                .order_by(models.Source.id)
            )
        )
        private = licenses.private_sources(session)
        marked = list(
            session.scalars(
                select(models.Source.id).where(models.Source.license_override.is_not(None))
            )
        )
        private_docs = int(
            session.scalar(
                select(func.count())
                .select_from(models.Document)
                .where(models.Document.license_class == LicenseClass.PRIVATE.value)
            )
            or 0
        )
        index, pages = _export_pages(session, sink, report)
        courses = _export_courses(session, settings.courses_repo, sink, report)
        exported_ids = {s.id for s in sources}
        registry = [
            {
                "id": s.id,
                "kind": s.kind,
                "title": s.title,
                "language": s.language,
                "version": s.version,
                "wing": s.wing,
                "status": s.status,
                "license_class": s.license_class,
                "meta": _safe_meta(s.meta, exported_ids),
            }
            for s in sources
        ]
        report.sources = len(registry)
        report.source_ids = sorted({*report.source_ids, *exported_ids})
        config = public_settings(settings)
        report.settings_keys = len(config)
        final_check(session, report, config)
        if private or marked or private_docs:
            report.excluded = {
                **report.excluded,
                "fontes_privadas": len({b.id for b in private} | set(marked)),
                "documentos_privados": private_docs,
            }
            licenses.record_filtered(
                session,
                "release externa",
                [*private, *(licenses.Blocked("source", m, m) for m in marked)],
            )
        revision = session.scalar(text("SELECT max(version_num) FROM alembic_version"))

    def dumps(value: Any) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False) + "\n"

    _put(sink, report, "config/settings.json", dumps(config))
    _put(sink, report, "registry/sources.json", dumps(registry))
    _put(sink, report, "wiki/INDEX.json", dumps(index))
    _put(sink, report, "CANARY.txt", canary + "\n")
    info = {"format": FORMAT, "created_at": stamp, "run_id": run_id, "counts": report.counts()}
    _put(sink, report, "db/info.csv", csv_bytes([["release", info]]))
    _put(
        sink,
        report,
        "db/source.csv",
        csv_bytes(
            [
                [s["id"], s["kind"], s["title"], s["language"], s["version"], s["wing"],
                 s["status"], s["license_class"], s["meta"]]
                for s in registry
            ]
        ),
    )  # fmt: skip
    _put(
        sink,
        report,
        "db/wiki_page.csv",
        csv_bytes(
            [
                [p["document_id"], p["source_id"], p["path"], p["title"], p["language"],
                 p["source_version"], p["fingerprint"], p["body"]]
                for p in pages
            ]
        ),
    )  # fmt: skip
    _put(
        sink,
        report,
        "db/course.csv",
        csv_bytes([[c["slug"], c["version"], c["source_id"]] for c in courses]),
    )
    release_info = {
        "format": FORMAT,
        "created_at": stamp,
        "ariadne_version": __version__,
        "schema_revision": revision,
        "run_id": run_id,
        "counts": report.counts(),
        "excluded": report.excluded,
        "note": "Open-class content only. User data, private content, original ZIMs and "
        "model weights are left out (STRUCTURE.md, section 16).",
    }
    _put(sink, report, "RELEASE.json", dumps(release_info))
    return report


# Os ficheiros de dados do restauro e a tabela de cada um (a ordem importa pouco: sem chaves
# estrangeiras). O restore.sh e o ensaio usam esta lista.
DATA_FILES = (
    ("info", "db/info.csv"),
    ("source", "db/source.csv"),
    ("wiki_page", "db/wiki_page.csv"),
    ("course", "db/course.csv"),
)


def new_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
