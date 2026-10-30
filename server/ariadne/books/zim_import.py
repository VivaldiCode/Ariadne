"""Livros que vêm dentro de um ZIM: do ZIM para o Calibre (`ariadne books import-zim`).

Duas origens, a mesma entrada (`add_book`, que recusa DRM, guarda no Calibre, extrai e indexa):

1. **ZIM da classe `book`** (gutenberg, wikibooks, wikisource…): percorre-se o ZIM à procura de
   EPUB, PDF, MOBI e DjVu. Cada um fica registado como documento com `meta.route = "book"`
   (`register_book_candidate`, a mesma marca do importador), que é onde se guarda o estado.
2. **PDF/EPUB dentro de ZIM de artigos**: o importador já os registou com `meta.route = "book"`;
   é por esses documentos que se começa.

**Duplicados.** O mesmo livro em vários formatos entra uma só vez, no melhor (EPUB, MOBI, PDF,
DjVu). O Project Gutenberg chama-lhes `Título.<id>.<ext>`: agrupa-se pelo `<id>`; nos outros ZIM,
pelo nome sem extensão na mesma pasta. Se o melhor formato falha (corrompido, DRM), tenta-se o
seguinte; só falha o livro quando falham todos. Repetir o comando não duplica nada: o documento de
cada ficheiro guarda `meta.book_id` e `meta.calibre_book_id` (os gémeos ignorados também, com
`meta.book_twin_of`), e o `add_book` reconhece o mesmo ficheiro pelo sha256 (`created=False`).
Um livro que o administrador removeu não volta: a marca fica. Uma falha fica em
`meta.book_import_error`; ao repetir, os livros ainda não tentados passam à frente dos falhados.

Uma falha num livro nunca pára o lote: fica no relatório.

**Todas as línguas, sem repetir a obra** (`books/works.py`, ARIAD-32). Antes de o livro ir para o
Calibre lê-se o OPF do EPUB (autor, título, língua, tradutor) e vê-se se a obra já existe na
língua do livro: um original entra sempre que a ranhura `(obra, língua)` esteja livre ou ocupada
por uma tradução; uma tradução só entra se a obra ainda não existir nessa língua; nunca duas
edições da mesma obra na mesma língua. O que a regra recusa fica marcado (`meta.book_refused`),
conta no relatório e não volta a ser tentado, a não ser com `retry_refused`.
"""

import json
import logging
import posixpath
import re
import tempfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ariadne.books.add import AddError, add_book
from ariadne.books.backend import LibraryError, LibraryUnavailable
from ariadne.books.extract import BookError, file_sha256, read_epub_credits
from ariadne.books.works import Decision, Work, WorkMeta, WorkRegistry, identify, language_code
from ariadne.db import models
from ariadne.documents import make_document_id, register_book_candidate
from ariadne.services import Services
from ariadne.sources.base import NotAnArticle
from ariadne.sources.classify import ContentClass
from ariadne.sources.license import LicenseClass
from ariadne.sources.zim import EntryInfo, ZimSource

logger = logging.getLogger(__name__)

# Do melhor para o pior: o EPUB dá capítulos e metadados; o MOBI converte-se para EPUB no Calibre;
# o PDF lê-se por páginas; o DjVu ainda não tem extrator (falha com a razão, não entra).
FORMAT_ORDER = ("epub", "mobi", "pdf", "djvu")
_MIMETYPES = {
    "application/epub+zip": "epub",
    "application/x-mobipocket-ebook": "mobi",
    "application/pdf": "pdf",
    "image/vnd.djvu": "djvu",
}
_EXTENSIONS = {f".{fmt}": fmt for fmt in FORMAT_ORDER}
_GUTENBERG_STEM = re.compile(r"^(?P<title>.+)\.(?P<id>\d+)$")
_UNSAFE = re.compile(r"[\x00-\x1f/\\]+")
MAX_NAME_BYTES = 200
GUTENBERG_URL = "https://www.gutenberg.org/ebooks/{id}"


class _Unfit(Exception):
    """O ficheiro não serve (grande demais, formato sem extrator): falha só este formato."""


class ZimImportError(Exception):
    """A fonte não serve para importar livros (não existe, não é um ZIM…)."""


class ZimImportAborted(ZimImportError):
    """A corrida parou a meio (o Calibre ou a base de dados em baixo, a fonte passou a privada).
    O que já entrou fica; `report` diz até onde se chegou."""

    def __init__(self, message: str, report: "ZimImportReport") -> None:
        super().__init__(message)
        self.report = report


def format_of(mimetype: str, path: str) -> str | None:
    """`epub`, `mobi`, `pdf` ou `djvu` (pelo tipo MIME ou pela extensão); `None` se não é livro."""
    fmt = _MIMETYPES.get((mimetype or "").split(";")[0].strip().lower())
    if fmt is not None:
        return fmt
    return _EXTENSIONS.get(posixpath.splitext(unquote(path).lower())[1])


@dataclass(frozen=True)
class Candidate:
    """Um ficheiro de livro dentro do ZIM."""

    path: str
    document_id: str
    format: str
    title: str  # o do nome do ficheiro (sem id nem extensão) ou o do ZIM
    group: str  # o que o liga aos outros formatos do mesmo livro
    gutenberg_id: str | None
    size: int


@dataclass(frozen=True)
class Imported:
    book_id: str
    title: str
    path: str
    format: str
    chapters: int
    twins: tuple[str, ...] = ()  # os outros formatos do mesmo livro, ignorados


@dataclass(frozen=True)
class Failure:
    path: str
    title: str
    reason: str


@dataclass(frozen=True)
class Refused:
    """Um livro que a regra das línguas recusou (a obra já existe nessa língua)."""

    path: str
    title: str
    language: str
    work: str
    reason: str
    kept: str  # o livro que ocupa a ranhura


@dataclass(frozen=True)
class Superseded:
    """Um original que entrou onde havia uma tradução da mesma obra: a tradução fica a mais."""

    book_id: str  # a tradução (para remover com `ariadne books remove`, se o dono quiser)
    by: str  # o original
    language: str


@dataclass
class ZimImportReport:
    source_id: str
    dry_run: bool = False
    files: int = 0  # ficheiros de livro encontrados
    books: int = 0  # livros distintos (depois de agrupar os formatos)
    added: list[Imported] = field(default_factory=list)
    existing: list[Imported] = field(default_factory=list)  # já estavam na biblioteca
    duplicates: list[str] = field(default_factory=list)  # caminhos ignorados (outro formato)
    skipped: int = 0  # livros tratados numa corrida anterior
    failures: list[Failure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # um formato falhou mas outro entrou
    pending: int = 0  # livros por tratar depois do limite
    refused: list[Refused] = field(default_factory=list)  # recusados pela regra das línguas
    refused_before: int = 0  # recusados numa corrida anterior (não se tentam sem `retry_refused`)
    superseded: list[Superseded] = field(default_factory=list)
    would_add: list[Imported] = field(default_factory=list)  # `dry_run`


Progress = Callable[[str], None]


# --- licença e atribuição --------------------------------------------------------------------

_LICENSES = (
    ("gutenberg", "Public domain (Project Gutenberg)"),
    ("wikibooks", "CC BY-SA (Wikibooks)"),
    ("wikisource", "Public domain or CC BY-SA (Wikisource)"),
)


def describe_source(zim: ZimSource, gutenberg_id: str | None) -> tuple[str, str]:
    """(nome da licença, atribuição) dos livros do ZIM, a partir dos metadados Name, Creator,
    Publisher e Source. Só o Gutenberg e os projetos Wikimedia têm licença conhecida; os outros
    ZIM ficam «aberta» com o nome do ZIM (o administrador pode dar outra com `--license-name`)."""
    name = str(zim.meta.get("zim_name") or zim.id.removeprefix("zim:"))
    lowered = name.casefold()
    license_name = next(
        (label for prefix, label in _LICENSES if lowered.startswith(prefix)),
        f"Open license (ZIM {name}; see the original source)",
    )
    parts: list[str] = []
    if gutenberg_id is not None:
        parts.append(f"Project Gutenberg ({GUTENBERG_URL.format(id=gutenberg_id)})")
    people = [
        value
        for value in dict.fromkeys(
            [str(zim.meta.get("creator") or ""), zim.zim_metadata("Publisher")]
        )
        if value.strip()
    ]
    if people:
        parts.append(" / ".join(people))
    parts.append(f"ZIM {name}")
    origin = zim.zim_metadata("Source").strip()
    if origin:
        parts.append(origin)
    return license_name, "; ".join(parts)


# --- candidatos ------------------------------------------------------------------------------


def _title_of(stem: str, zim_title: str) -> str:
    cleaned = " ".join(stem.replace("_", " ").split())
    if cleaned:
        return cleaned
    return " ".join(zim_title.split())


def _candidate(zim: ZimSource, info: EntryInfo, fmt: str, gutenberg: bool) -> Candidate:
    directory, filename = posixpath.split(info.path)
    stem = posixpath.splitext(unquote(filename))[0]
    match = _GUTENBERG_STEM.match(stem) if gutenberg else None
    if match is not None:
        gid: str | None = match["id"]
        group, title = f"gutenberg:{gid}", _title_of(match["title"], info.title)
    else:
        gid = None
        group, title = f"{directory}/{stem.casefold()}", _title_of(stem, info.title)
    return Candidate(
        path=info.path,
        document_id=make_document_id(zim.id, info.path),
        format=fmt,
        title=title,
        group=group,
        gutenberg_id=gid,
        size=info.size,
    )


def _registered_paths(session: Session, source_id: str) -> list[str]:
    return list(
        session.scalars(
            select(models.Document.path)
            .where(
                models.Document.source_id == source_id,
                models.Document.meta["route"].astext == "book",
            )
            .order_by(models.Document.path)
        )
    )


def find_candidates(
    zim: ZimSource, session: Session, *, register: bool
) -> tuple[list[Candidate], list[str]]:
    """Os ficheiros de livro da fonte e os avisos. Num ZIM de livros percorre-se o ZIM inteiro
    (e, com `register`, regista-se o que o importador ainda não registou); noutros, só os
    documentos que o importador marcou com `meta.route = "book"`."""
    gutenberg = str(zim.meta.get("zim_name", "")).casefold().startswith("gutenberg")
    found: dict[str, Candidate] = {}
    warnings: list[str] = []
    if zim.content_class_of() is ContentClass.BOOK:
        for info in zim.entry_infos(lambda mime, path: format_of(mime, path) is not None):
            fmt = format_of(info.mimetype, info.path)
            if fmt is None:
                continue
            found[info.path] = _candidate(zim, info, fmt, gutenberg)
            if register:
                register_book_candidate(
                    session,
                    zim,
                    info.path,
                    NotAnArticle("", path=info.path, mimetype=info.mimetype, title=info.title),
                )
    for path in _registered_paths(session, zim.id):
        if path in found:
            continue
        try:
            info = zim.entry_info(path)
        except Exception as exc:  # o documento já não está neste ZIM (versão nova)
            warnings.append(f"{path}: no longer in the ZIM ({exc})")
            continue
        fmt = format_of(info.mimetype, info.path)
        if fmt is not None:
            found[path] = _candidate(zim, info, fmt, gutenberg)
    return sorted(found.values(), key=lambda c: c.path), warnings


# --- estado ----------------------------------------------------------------------------------


def _markers(session: Session, document_ids: list[str]) -> dict[str, dict[str, Any]]:
    """`meta` dos documentos que têm marca de importação (`book_id`, `book_import_error` ou
    `book_refused`)."""
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(document_ids), 1000):
        chunk = document_ids[start : start + 1000]
        for document_id, meta in session.execute(
            select(models.Document.id, models.Document.meta).where(models.Document.id.in_(chunk))
        ):
            if "book_id" in meta or "book_import_error" in meta or "book_refused" in meta:
                out[document_id] = meta
    return out


def _patch(
    session: Session, document_id: str, patch: dict[str, Any], drop: tuple[str, ...] = ()
) -> None:
    session.execute(
        text(
            "UPDATE document SET meta = (meta - CAST(:drop AS text[])) || CAST(:patch AS jsonb) "
            "WHERE id = :id"
        ),
        {"patch": json.dumps(patch), "id": document_id, "drop": list(drop)},
    )


_CLEARED = ("book_import_error", "book_refused", "book_work")  # as marcas de antes de entrar


def _mark_done(
    services: Services,
    primary: Candidate,
    twins: list[Candidate],
    book_id: str,
    calibre_id: Any,
    *,
    mark_primary: bool = True,
) -> None:
    """Guarda `book_id` e `calibre_book_id` no documento do ficheiro importado e, nos outros
    formatos do mesmo livro, também (com `book_twin_of`), para não voltarem a ser tentados."""
    base: dict[str, Any] = {"book_id": book_id, "calibre_book_id": calibre_id}
    with services.sessions() as session:
        if mark_primary:
            _patch(session, primary.document_id, base, drop=_CLEARED)
        for twin in twins:
            _patch(
                session,
                twin.document_id,
                {**base, "book_twin_of": primary.document_id},
                drop=_CLEARED,
            )
        session.commit()


def _mark_failed(services: Services, candidate: Candidate, reason: str) -> None:
    with services.sessions() as session:
        _patch(session, candidate.document_id, {"book_import_error": reason[:500]})
        session.commit()


# --- importação ------------------------------------------------------------------------------


def _rank(candidate: Candidate) -> int:
    return FORMAT_ORDER.index(candidate.format)


def _write_temp(directory: Path, zim: ZimSource, candidate: Candidate) -> Path:
    _, content = zim.read_bytes(candidate.path)
    stem = _UNSAFE.sub(" ", posixpath.splitext(unquote(posixpath.basename(candidate.path)))[0])
    # Corta por bytes UTF-8 (o limite dos sistemas de ficheiros), sem partir um carácter.
    name = stem.strip().encode()[:MAX_NAME_BYTES].decode(errors="ignore").strip()
    file = directory / f"{name or 'livro'}.{candidate.format}"
    file.write_bytes(content)
    return file


def _reason(exc: BaseException) -> str:
    text_ = str(exc).strip() or type(exc).__name__
    return " ".join(text_.split())[:300]


def import_zim_books(
    services: Services,
    source_id: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    license_name: str | None = None,
    retry_refused: bool = False,
    progress: Progress | None = None,
) -> ZimImportReport:
    """Leva os livros da fonte ZIM `source_id` para o Calibre (e o índice). `limit` conta livros
    por tratar nesta corrida (os já tratados não contam); `dry_run` só diz o que faria (e que livros
    a regra das línguas recusaria); `retry_refused` volta a avaliar os que ela recusou antes."""
    say: Progress = progress or (lambda _line: None)
    with services.sessions() as session:
        services.registry.sync_if_changed(session)
        session.commit()
    zim = services.registry.get(source_id)
    if zim is None:
        raise ZimImportError(f"Unknown source: {source_id!r} (see `ariadne sources`).")
    if not isinstance(zim, ZimSource):
        raise ZimImportError(f"{source_id!r} is not a ZIM ({zim.kind}).")
    if zim.license_class is not LicenseClass.OPEN:
        raise ZimImportError(
            f"{source_id!r} is {zim.license_class.value}: "
            "only books from open sources are imported."
        )

    report = ZimImportReport(source_id, dry_run=dry_run)
    with services.sessions() as session:
        candidates, report.warnings = find_candidates(zim, session, register=not dry_run)
        session.commit()
        markers = _markers(session, [c.document_id for c in candidates])
        known_books = set(session.scalars(select(models.Book.id)))
        registry = WorkRegistry.load(session)
    report.files = len(candidates)

    groups: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[candidate.group].append(candidate)
    report.books = len(groups)

    todo: list[tuple[bool, str, list[Candidate]]] = []
    for key, members in groups.items():
        members.sort(key=lambda c: (_rank(c), c.path))
        done = next(
            (
                m
                for m in members
                if (meta := markers.get(m.document_id)) and meta.get("book_id") in known_books
            ),
            None,
        )
        if done is not None:
            report.skipped += 1
            pending = [m for m in members if "book_id" not in markers.get(m.document_id, {})]
            if pending and not dry_run:
                book_id = str(markers[done.document_id]["book_id"])
                calibre_id = markers[done.document_id].get("calibre_book_id")
                _mark_done(services, done, pending, book_id, calibre_id, mark_primary=False)
            continue
        if not retry_refused and any(
            "book_refused" in markers.get(m.document_id, {}) for m in members
        ):
            report.refused_before += 1
            continue
        failed_before = any("book_import_error" in markers.get(m.document_id, {}) for m in members)
        todo.append((failed_before, key, members))
    todo.sort(key=lambda item: (item[0], item[1]))  # os ainda não tentados primeiro
    if limit is not None and len(todo) > limit:
        report.pending = len(todo) - limit
        todo = todo[: max(limit, 0)]

    try:
        with tempfile.TemporaryDirectory(prefix="ariadne-zim-livros-") as scratch:
            for _failed, _key, members in todo:
                current = LicenseClass.OPEN if dry_run else _current_class(services, source_id)
                if current is not LicenseClass.OPEN:
                    raise ZimImportAborted(
                        f"{source_id!r} became {current.value} during the run: I stopped (a book "
                        "from a private source never enters as open). What already got in "
                        "stays in the library.",
                        report,
                    )
                _import_group(
                    services, zim, members, Path(scratch), report, license_name, say, current,
                    registry,
                )  # fmt: skip
    except SQLAlchemyError as exc:
        raise ZimImportAborted(
            f"Database unavailable ({type(exc).__name__}); repeat the command.", report
        ) from exc
    return report


def _current_class(services: Services, source_id: str) -> LicenseClass:
    """A classe da fonte agora, na base de dados: a mais restritiva entre `license_class` e a
    marca `license_override` (uma fonte pode ter sido marcada privada depois de a corrida
    começar). Uma fonte que desapareceu conta como privada."""
    with services.sessions() as session:
        row = session.get(models.Source, source_id)
        if row is None or row.status != "active":
            return LicenseClass.PRIVATE
        if row.license_class != "open" or row.license_override == "private":
            return LicenseClass.PRIVATE
        return LicenseClass.OPEN


def _import_group(
    services: Services,
    zim: ZimSource,
    members: list[Candidate],
    scratch: Path,
    report: ZimImportReport,
    license_override: str | None,
    say: Progress,
    license_class: LicenseClass,
    registry: WorkRegistry,
) -> None:
    best = members[0]
    license_name, attribution = describe_source(zim, best.gutenberg_id)
    if license_override:
        license_name = license_override
    zim_language = language_code(str(zim.meta.get("zim_language") or zim.language or ""))
    if report.dry_run:
        work, decision = _judge(services, zim, best, scratch, registry, zim_language)
        if decision.kind == "refuse":
            _report_refusal(report, best, work, decision, say)
            return
        registry.record(work, f"(dry run) {best.title}")
        report.would_add.append(
            Imported("", best.title, best.path, best.format, 0, tuple(m.path for m in members[1:]))
        )
        report.duplicates.extend(m.path for m in members[1:])
        say(
            f"  {best.title!r}: {best.path}"
            + (f" (+{len(members) - 1} formats)" if members[1:] else "")
        )
        return
    attempts: list[str] = []
    refusal: tuple[Work | None, Decision] | None = None
    for position, candidate in enumerate(members):
        try:
            limit = services.settings.books_max_bytes
            if candidate.size > limit:
                raise _Unfit(
                    f"file too large ({candidate.size} bytes; maximum {limit}, BOOKS_MAX_BYTES)"
                )
            if candidate.format == "djvu":
                raise _Unfit("DjVu has no extractor yet (use EPUB or PDF)")
            file = _write_temp(scratch, zim, candidate)
            try:
                work, decision = _decide(services, file, candidate, zim_language, registry)
                if decision.kind == "refuse":
                    refusal = (work, decision)
                    break
                result = add_book(
                    services,
                    file,
                    license_class=license_class,
                    license_name=license_name,
                    attribution=attribution,
                    # um PDF costuma vir sem metadados: o título do nome do ficheiro
                    title=candidate.title if candidate.format == "pdf" else None,
                )
            finally:
                file.unlink(missing_ok=True)
        except Exception as exc:
            if _is_infrastructure(exc):  # não é culpa do livro: a corrida pára, sem o marcar
                raise ZimImportAborted(
                    f"{type(exc).__name__}: {_reason(exc)} — stopped at {candidate.path!r}; "
                    "the book was not marked as failed.",
                    report,
                ) from exc
            logger.warning("%s: %s", candidate.path, exc, exc_info=not _expected(exc))
            attempts.append(f"{candidate.format.upper()}: {_reason(exc)}")
            continue
        twins = members[position + 1 :]  # os formatos piores, ignorados
        _mark_done(services, candidate, twins, result.book_id, result.calibre_id)
        if result.created:
            _store_work(services, result.book_id, work, decision, report)
            registry.record(work, result.book_id)
        entry = Imported(
            result.book_id,
            result.title,
            candidate.path,
            candidate.format,
            result.chapters,
            tuple(t.path for t in twins),
        )
        (report.added if result.created else report.existing).append(entry)
        report.duplicates.extend(t.path for t in twins)
        report.warnings.extend(f"{candidate.title!r}: {a}" for a in attempts)
        say(
            f"  {'+' if result.created else '='} {result.book_id}: {result.title!r} "
            f"({candidate.format.upper()}, {result.chapters} chapters)"
        )
        return
    if refusal is not None:  # a regra das línguas: a obra já existe nesta língua
        work, decision = refusal
        _report_refusal(report, best, work, decision, say)
        _mark_refused(services, members, work, decision.reason)
        return
    reason = "; ".join(attempts)
    report.failures.append(Failure(best.path, best.title, reason))
    _mark_failed(services, best, reason)
    say(f"  ! {best.title!r}: {reason}")


# --- a regra das línguas -----------------------------------------------------------------------


def _work_of(
    services: Services, file: Path, candidate: Candidate, zim_language: str | None
) -> Work | None:
    """A obra do ficheiro, lida do OPF do EPUB. Os outros formatos e os EPUB ilegíveis não têm
    metadados de que a regra se fie (o PDF não traz autor): entram sempre (`None`)."""
    if candidate.format != "epub":
        return None
    try:
        credits = read_epub_credits(file)
    except BookError:
        return None  # o `add_book` dirá porque não serve
    meta = WorkMeta.from_credits(credits, zim_language)
    if not meta.title.strip():
        meta = replace(meta, title=candidate.title)
    with services.sessions() as session:
        return identify(session, meta)


def _decide(
    services: Services,
    file: Path,
    candidate: Candidate,
    zim_language: str | None,
    registry: WorkRegistry,
) -> tuple[Work | None, Decision]:
    """A obra e a decisão. Um ficheiro que já está na biblioteca (mesmo sha256) não é julgado:
    o `add_book` reconhece-o e a ranhura que ocupa é a dele."""
    work = _work_of(services, file, candidate, zim_language)
    if work is None:
        return None, Decision("admit")
    with services.sessions() as session:
        known = session.scalar(
            select(models.Book.id).where(
                models.Book.file_sha256 == file_sha256(file), models.Book.status == "active"
            )
        )
    return work, Decision("admit") if known else registry.decide(work)


def _judge(
    services: Services,
    zim: ZimSource,
    candidate: Candidate,
    scratch: Path,
    registry: WorkRegistry,
    zim_language: str | None,
) -> tuple[Work | None, Decision]:
    """O que a regra diz do livro, sem o importar (`--dry-run`)."""
    if candidate.format != "epub" or candidate.size > services.settings.books_max_bytes:
        return None, Decision("admit")
    file = _write_temp(scratch, zim, candidate)
    try:
        return _decide(services, file, candidate, zim_language, registry)
    finally:
        file.unlink(missing_ok=True)


def _report_refusal(
    report: ZimImportReport,
    candidate: Candidate,
    work: Work | None,
    decision: Decision,
    say: Progress,
) -> None:
    assert work is not None and decision.holder is not None
    report.refused.append(
        Refused(
            candidate.path,
            candidate.title,
            work.language,
            work.key,
            decision.reason,
            decision.holder.book_id,
        )
    )
    say(f"  - {candidate.title!r}: refused, {decision.reason}")


def _mark_refused(
    services: Services, members: list[Candidate], work: Work | None, reason: str
) -> None:
    """Marca os documentos do livro como recusados pela regra: não se tentam outra vez (cada
    corrida seria uma releitura inútil dos mesmos ficheiros) até `retry_refused`."""
    patch = {"book_refused": reason[:500], "book_work": work.key if work else None}
    with services.sessions() as session:
        for member in members:
            _patch(session, member.document_id, patch, drop=("book_import_error",))
        session.commit()


def _store_work(
    services: Services,
    book_id: str,
    work: Work | None,
    decision: Decision,
    report: ZimImportReport,
) -> None:
    """Guarda a obra no livro (`book.meta.work`) e, se um original tomou o lugar de uma tradução,
    marca-a e conta-a no relatório."""
    if work is None:
        return
    with services.sessions() as session:
        _patch_book(session, book_id, {"work": work.as_meta()})
        if decision.kind == "supersede" and decision.holder is not None:
            _patch_book(session, decision.holder.book_id, {"work_superseded_by": book_id})
            report.superseded.append(Superseded(decision.holder.book_id, book_id, work.language))
        session.commit()


def _patch_book(session: Session, book_id: str, patch: dict[str, Any]) -> None:
    session.execute(
        text("UPDATE book SET meta = meta || CAST(:patch AS jsonb) WHERE id = :id"),
        {"patch": json.dumps(patch), "id": book_id},
    )


def _is_infrastructure(exc: BaseException) -> bool:
    """O Calibre inacessível ou a base de dados em baixo (o `add_book` embrulha o primeiro num
    `AddError`, daí percorrer a cadeia de causas)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, LibraryUnavailable | SQLAlchemyError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _expected(exc: BaseException) -> bool:
    """Falhas esperadas (DRM, ficheiro estragado, Calibre a recusar) não pedem traceback."""
    return isinstance(exc, AddError | LibraryError | BookError | _Unfit)
