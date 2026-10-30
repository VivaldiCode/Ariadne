"""Documentos: IDs estáveis, leitura (`read`), catálogo de fontes e feedback.

`document.id = <source_id>/<path>` (ARCHITECTURE.md, secção 14, ponto 1): a versão da fonte fica
numa coluna à parte, por isso atualizar um ZIM não parte referências nem estatísticas.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import unquote

from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne import heat
from ariadne.books.info import BookInfo, book_infos, citation
from ariadne.courses.info import LessonInfo, lesson_info
from ariadne.curation import flags as flagging
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import QueryLog, QueryResult
from ariadne.kiwix import content_url
from ariadne.relink import RELINK, relink_when
from ariadne.rooms import RoomRef, rooms_of
from ariadne.services import Services
from ariadne.sources.base import (
    Article,
    Link,
    NotAnArticle,
    NotFound,
    Section,
    Source,
)
from ariadne.textutil import clean_text, title_key
from ariadne.wiki import convert, doctype

DEFAULT_MAX_CHARS = 20_000
MIN_MAX_CHARS = 100
LIMIT_MAX_CHARS = 200_000
MAX_LINKS = 100
# Um `read` marca como 'usado' o resultado de uma pesquisa feita há menos de isto.
USED_WINDOW_HOURS = 6
# Títulos alternativos (redirects) guardados por documento; há artigos com centenas.
MAX_ALIASES = 50


INVALID_ARGUMENT = "Invalid argument: the value has characters the database does not accept."


class LibraryError(Exception):
    """Erro que o agente pode corrigir: a mensagem é para ele ler."""


class UnknownSource(LibraryError):
    pass


class DocumentNotFound(LibraryError):
    pass


class SectionNotFound(LibraryError):
    pass


class FeedbackError(LibraryError):
    pass


# --- IDs ------------------------------------------------------------------------------------


def make_document_id(source_id: str, path: str) -> str:
    return f"{source_id}/{path}"


def split_document_id(document_id: str) -> tuple[str, str]:
    """'zim:wikipedia_pt/Lisboa' -> ('zim:wikipedia_pt', 'Lisboa'). O caminho pode ter '/'."""
    source_id, sep, path = document_id.partition("/")
    if not sep or not path:
        raise DocumentNotFound(
            f"Invalid document_id: {document_id!r}. Use the ids returned by search "
            "(format '<source_id>/<path>')."
        )
    return source_id, path


@dataclass(frozen=True)
class DocumentInfo:
    source: Source
    path: str
    title: str
    aliases: tuple[str, ...] = ()  # títulos alternativos vistos (redirects), para o `title_match`


def document_info(source: Source, article: Article) -> DocumentInfo:
    """O documento de um artigo lido; se o pedido foi um redirect, o título dele é um alias."""
    aliases = (article.redirected_title,) if article.redirected_title else ()
    return DocumentInfo(source, article.path, article.title, aliases)


def upsert_documents(session: Session, documents: list[DocumentInfo]) -> None:
    """Regista os documentos vistos (nunca apaga). Repetições no mesmo pedido são fundidas."""
    rows: dict[str, dict[str, Any]] = {}
    for document in documents:
        document_id = make_document_id(document.source.id, document.path)
        rows[document_id] = {
            "id": document_id,
            "source_id": document.source.id,
            "path": document.path,
            "version": document.source.version,
            "title": document.title,
            "language": document.source.language,
            "content_type": document.source.content_type,
            # A classe herda-se da fonte (STRUCTURE.md, secção 7).
            "license_class": document.source.license_class.value,
        }
    if not rows:
        return
    # Por ordem de id: pesquisas concorrentes bloqueiam as linhas na mesma ordem (sem deadlock).
    stmt = insert(DocumentRow).values([rows[key] for key in sorted(rows)])
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "version": stmt.excluded.version,
                "title": stmt.excluded.title,
                "language": stmt.excluded.language,
                "content_type": stmt.excluded.content_type,
                "license_class": stmt.excluded.license_class,
                # Mudou o título: as chaves do `title_match` deixaram de valer, o linker revê-o.
                **relink_when(DocumentRow.title.is_distinct_from(stmt.excluded.title)),
            },
        )
    )
    add_aliases(
        session,
        {make_document_id(d.source.id, d.path): list(d.aliases) for d in documents if d.aliases},
    )


def _title_from_path(path: str) -> str:
    """Um título legível para um ficheiro sem título no ZIM: o nome sem pasta nem extensão."""
    name = unquote(path).rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)
    return " ".join(name.replace("_", " ").split()) or path


def register_book_candidate(
    session: Session, source: Source, requested: str, exc: NotAnArticle
) -> tuple[str, doctype.DocKind] | None:
    """Um PDF/EPUB/DjVu/MOBI dentro do ZIM não é um artigo, é um livro candidato para o Calibre.
    Regista a linha `document` (se não existia) com `meta.route = "book"` e o motivo em
    `meta.wiki_skip` (o varrimento do importador não a volta a enfileirar), e devolve o id do
    documento e o tipo. `None` se o item não é um livro (imagem, CSS…). Não faz commit."""
    path = exc.path or requested
    kind = doctype.book_by_format(exc.mimetype, unquote(path))
    if kind is None:
        return None
    title = " ".join(exc.title.split())
    if not title or "/" in title or title.casefold().endswith((".pdf", ".epub", ".djvu", ".mobi")):
        title = _title_from_path(path)  # o ZIM não deu título: o do item é o próprio caminho
    upsert_documents(session, [DocumentInfo(source, path, title)])
    document_id = make_document_id(source.id, path)
    session.execute(
        text("UPDATE document SET meta = meta || CAST(:patch AS jsonb) WHERE id = :id"),
        {"patch": json.dumps({"route": "book", "wiki_skip": kind.reason}), "id": document_id},
    )
    return document_id, kind


def add_aliases(session: Session, aliases: dict[str, list[str]]) -> int:
    """Junta títulos alternativos (de redirects) a `document.meta.aliases`, sem repetir nem
    guardar o próprio título; o que muda volta à fila do `linker`. Devolve quantos documentos
    mudaram. Os documentos têm de existir."""
    if not aliases:
        return 0
    rows = session.execute(
        select(DocumentRow.id, DocumentRow.title, DocumentRow.meta)
        .where(DocumentRow.id.in_(sorted(aliases)))
        .order_by(DocumentRow.id)
        .with_for_update()
    ).all()
    changed = 0
    for document_id, title, meta in rows:
        known: list[str] = list(meta.get("aliases", []))
        seen = {title_key(title), *map(title_key, known)}
        added: list[str] = []
        for alias in aliases[document_id]:
            alias = clean_text(alias).strip()
            key = title_key(alias)
            if key and key not in seen and len(known) + len(added) < MAX_ALIASES:
                seen.add(key)
                added.append(alias)
        if added:
            session.execute(
                update(DocumentRow)
                .where(DocumentRow.id == document_id)
                .values(meta={**meta, "aliases": [*known, *added]}, **RELINK)
            )
            changed += 1
    return changed


# --- ler ------------------------------------------------------------------------------------


class SectionInfo(BaseModel):
    title: str
    level: int  # 1 = introdução, 2 e 3 = títulos do artigo
    chars: int


class LinkInfo(BaseModel):
    document_id: str
    title: str


class DocumentView(BaseModel):
    document_id: str
    title: str
    source_id: str
    language: str | None
    version: str
    wing: str | None = None  # a ala da fonte
    room: RoomRef | None = None  # a sala do documento, se já foi calculada
    section: str | None  # título da secção devolvida, se foi pedida uma
    text: str
    truncated: bool
    total_chars: int  # tamanho do texto completo (da secção, se foi pedida)
    sections: list[SectionInfo]
    links: list[LinkInfo]
    redirected_from: str | None = None
    kiwix_url: str | None = None
    wiki_url: str | None = None
    # Códigos da triagem (ex.: injection, hidden_text, dangerous_command) sobre o texto do artigo.
    flags: list[str] = []
    # O que o documento é: `article`, `lesson` (curso) ou `chapter` (livro); nas lições, o curso,
    # os pré-requisitos e os exercícios.
    content_type: str = "article"
    lesson: LessonInfo | None = None
    # Nos capítulos: o livro (com o capítulo anterior e o seguinte, e o leitor), o deslocamento a
    # que o texto começa (`offset`) e a citação dessa posição: `book:<livro>/<capítulo>#<posição>`.
    book: BookInfo | None = None
    offset: int = 0
    citation: str | None = None

    def call_summary(self) -> dict[str, Any]:
        """O que fica em `tool_call.result`: ids e contagens, nunca o texto."""
        return {
            "document_id": self.document_id,
            "section": self.section,
            "chars": len(self.text),
            "truncated": self.truncated,
            "links": len(self.links),
            "flags": self.flags,
        }


def _normalize(title: str) -> str:
    return re.sub(r"\s+", " ", title.lstrip("# ").strip()).casefold()


def find_section(article: Article, name: str) -> Section:
    """Secção pelo título (sem maiúsculas): igual, depois começa por, depois contém."""
    wanted = _normalize(name)
    titles = [(section, _normalize(section.title)) for section in article.sections]
    if wanted:
        for matches in (
            lambda title: title == wanted,
            lambda title: title.startswith(wanted),
            lambda title: wanted in title,
        ):
            for section, title in titles:
                if matches(title):
                    return section
    available = ", ".join(repr(s.title) for s in article.sections) or "(no sections)"
    raise SectionNotFound(f"Section {name!r} not found. Available sections: {available}.")


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Corta em `max_chars`, de preferência no fim de um parágrafo."""
    if len(text) <= max_chars:
        return text, False
    cut = text.rfind("\n\n", 0, max_chars)
    if cut < max_chars // 2:
        cut = text.rfind(" ", 0, max_chars)
    if cut < max_chars // 2:
        cut = max_chars
    return text[:cut].rstrip(), True


def _unique(links: list[Link]) -> list[Link]:
    """Uma ligação por artigo, pela ordem em que aparecem."""
    first: dict[str, Link] = {}
    for link in links:
        first.setdefault(link.path, link)
    return list(first.values())


def read_document(
    services: Services,
    session: Session,
    principal_id: int,
    document_id: str,
    *,
    section: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    session_id: str | None = None,
    offset: int = 0,
) -> DocumentView:
    """Lê um documento da fonte (libzim), regista-o em `document` e marca-o como usado.
    `offset` salta os primeiros caracteres do texto (para continuar um capítulo longo a partir da
    posição de uma citação)."""
    document_id = clean_text(document_id)
    section = clean_text(section) if section else section
    max_chars = max(MIN_MAX_CHARS, min(max_chars, LIMIT_MAX_CHARS))
    services.registry.sync_if_changed(session)
    session.commit()  # liberta a ligação antes do trabalho lento (kiwix, libzim)
    source_id, path = split_document_id(document_id)
    source = services.registry.get(source_id)
    if source is None:
        known = ", ".join(s.id for s in services.registry.active()) or "(none)"
        raise UnknownSource(f"Unknown source: {source_id!r}. Available sources: {known}.")
    try:
        article = read_article(source, path)
    except NotFound as exc:
        raise DocumentNotFound(f"Document not found: {document_id!r}.") from exc
    except NotAnArticle as exc:
        raise DocumentNotFound(f"{document_id!r} is not a text article ({exc}).") from exc

    chosen = find_section(article, section) if section else None
    text = article.section_text(chosen) if chosen else article.text
    offset = max(0, min(offset, len(text)))
    shown, truncated = truncate(text[offset:], max_chars)
    links = article.section_links(chosen) if chosen else list(article.links)
    book = source.meta.get("kiwix_book")
    canonical_id = make_document_id(source.id, article.path)
    redirected_from = article.redirected_from

    upsert_documents(session, [document_info(source, article)])
    wiki_url = wiki_urls_for(session, services, [canonical_id]).get(canonical_id)
    book_info = (
        book_infos(
            session, [canonical_id], services.settings.kavita_public_url, neighbors=True
        ).get(canonical_id)
        if source.content_type == "chapter"
        else None
    )
    chunk_ids = _mark_used(session, principal_id, canonical_id, session_id)
    half_life = services.settings.heat_half_life_days
    heat.add_document_heat(session, [canonical_id], heat.WEIGHT_USED, half_life)
    heat.add_chunk_heat(session, chunk_ids, heat.WEIGHT_USED, half_life)
    session.commit()

    return DocumentView(
        document_id=canonical_id,
        title=article.title,
        source_id=source.id,
        language=source.language,
        version=source.version,
        wing=source.wing,
        room=rooms_of(session, [canonical_id]).get(canonical_id),
        section=chosen.title if chosen else None,
        text=shown,
        truncated=truncated,
        total_chars=len(text),
        sections=[
            SectionInfo(title=s.title, level=s.level, chars=len(article.section_text(s)))
            for s in article.sections
        ],
        links=[
            LinkInfo(document_id=make_document_id(source.id, link.path), title=link.title)
            for link in _unique(links)[:MAX_LINKS]
        ],
        redirected_from=make_document_id(source.id, redirected_from) if redirected_from else None,
        kiwix_url=(
            content_url(services.settings.kiwix_public_url, book, article.path) if book else None
        ),
        wiki_url=wiki_url,
        flags=screen(article, source.id, services),
        content_type=source.content_type,
        lesson=lesson_info(session, canonical_id) if source.content_type == "lesson" else None,
        book=book_info,
        offset=offset,
        citation=(
            citation(canonical_id, offset) if book_info is not None and chosen is None else None
        ),
    )


def screen(article: Article, source_id: str, services: Services) -> list[str]:
    """Os códigos da triagem sobre o artigo (HTML e texto), para avisar quem o lê."""
    root, hidden = convert.prepare(article.html)
    found = flagging.detect(
        article.text,
        code=flagging.code_texts(root),
        hidden=hidden,
        settings=services.settings,
        source_id=source_id,
    )
    return sorted({f.code for f in found})


def wiki_urls_for(
    session: Session, services: Services, document_ids: Sequence[str]
) -> dict[str, str]:
    """document_id -> URL público da página do Wiki.js, para os que já têm página."""
    if not document_ids:
        return {}
    base = services.settings.wikijs_public_url.rstrip("/")
    rows = session.execute(
        select(DocumentRow.id, DocumentRow.meta["wiki_path"].astext).where(
            DocumentRow.id.in_(list(document_ids)), DocumentRow.wiki_page_id.is_not(None)
        )
    )
    return {document_id: f"{base}/{path}" for document_id, path in rows if path}


def read_article(source: Source, path: str) -> Article:
    """Lê o caminho; se não existe, tenta as grafias que um agente costuma copiar dos links."""
    attempts = list(dict.fromkeys([path, unquote(path), unquote(path).replace(" ", "_")]))
    for attempt in attempts[:-1]:
        try:
            return source.read(attempt)
        except NotFound:
            continue
    return source.read(attempts[-1])


def _mark_used(
    session: Session, principal_id: int, document_id: str, session_id: str | None
) -> list[int]:
    """O agente abriu um resultado de uma pesquisa recente dele: `query_result.used = true`.
    Devolve os chunks (nível 3) desses resultados, que também aquecem."""
    recent = select(QueryLog.id).where(
        QueryLog.principal_id == principal_id,
        QueryLog.created_at > func.now() - timedelta(hours=USED_WINDOW_HOURS),
    )
    if session_id is not None:
        recent = recent.where(QueryLog.session_id == session_id)
    chunk_ids = session.scalars(
        update(QueryResult)
        .where(QueryResult.document_id == document_id, QueryResult.query_id.in_(recent))
        .where(QueryResult.used.is_distinct_from(True))
        .values(used=True)
        .returning(QueryResult.chunk_id)
    ).all()
    return [chunk_id for chunk_id in chunk_ids if chunk_id is not None]
