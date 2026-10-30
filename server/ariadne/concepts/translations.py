"""`translations`: as versões do mesmo conceito noutras línguas (ARCHITECTURE.md, secção 11.1).

O conceito (`document.concept_id`) agrupa as versões do mesmo tema; aqui lista-se as que estão
em fontes ativas e noutra língua que não a do documento, com a marca `original_language` na
versão que está na língua original do conceito (`concept.origin_lang`, quando se sabe). Uma versão
cuja ligação foi rejeitada na curadoria (aresta `langlink`/`wikidata` rejeitada) não aparece.
"""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from ariadne.db.models import Concept, Link
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Source as SourceRow
from ariadne.documents import DocumentNotFound, wiki_urls_for
from ariadne.kiwix import content_url
from ariadne.services import Services
from ariadne.textutil import clean_text

LANGUAGE_KINDS = ("langlink", "wikidata")


class ConceptRef(BaseModel):
    id: int
    wikidata_qid: str | None
    label: str | None
    origin_lang: str | None  # None: não se sabe qual é a língua original


class TranslationInfo(BaseModel):
    document_id: str
    title: str
    source_id: str
    language: str | None
    original_language: bool  # está na língua original do conceito: a «fonte original»
    wiki_url: str | None = None
    kiwix_url: str | None = None


class TranslationsResult(BaseModel):
    document_id: str
    title: str
    language: str | None
    concept: ConceptRef | None  # None: o documento ainda não está ligado a nenhum conceito
    is_original: bool = False  # o próprio documento está na língua original
    translations: list[TranslationInfo]
    flags: list[str] = []  # no MCP: `title_injection` se algum título parece instruções

    def call_summary(self) -> dict[str, Any]:
        """O que fica em `tool_call.result`: ids e contagens."""
        return {
            "document_id": self.document_id,
            "concept_id": self.concept.id if self.concept else None,
            "count": len(self.translations),
            "document_ids": [t.document_id for t in self.translations],
            "original": [t.document_id for t in self.translations if t.original_language],
        }


def translations(services: Services, session: Session, document_id: str) -> TranslationsResult:
    document_id = clean_text(document_id)
    document = session.get(DocumentRow, document_id)
    if document is None:
        raise DocumentNotFound(
            f"Unknown document: {document_id!r}. Use the document_ids from `search` or `read`."
        )
    concept = session.get(Concept, document.concept_id) if document.concept_id else None
    if concept is None:
        return TranslationsResult(
            document_id=document.id,
            title=document.title,
            language=document.language,
            concept=None,
            translations=[],
        )
    origin = concept.origin_lang
    other = aliased(DocumentRow)
    rejected = exists().where(
        Link.kind.in_(LANGUAGE_KINDS),
        Link.status == "rejected",
        or_(
            and_(Link.src_document == document.id, Link.dst_document == other.id),
            and_(Link.dst_document == document.id, Link.src_document == other.id),
        ),
    )
    query = (
        select(other, SourceRow)
        .join(SourceRow, SourceRow.id == other.source_id)
        .where(
            other.concept_id == concept.id,
            other.id != document.id,
            SourceRow.status == "active",
            ~rejected,
        )
    )
    if document.language is not None:
        query = query.where(other.language.is_distinct_from(document.language))
    rows = session.execute(query).all()
    ids = [row.id for row, _ in rows]
    wiki_urls = wiki_urls_for(session, services, ids)
    public = services.settings.kiwix_public_url
    found = []
    for row, source in rows:
        book = source.meta.get("kiwix_book")
        found.append(
            TranslationInfo(
                document_id=row.id,
                title=row.title,
                source_id=row.source_id,
                language=row.language,
                original_language=origin is not None and row.language == origin,
                wiki_url=wiki_urls.get(row.id),
                kiwix_url=content_url(public, str(book), row.path) if book else None,
            )
        )
    found.sort(key=lambda t: (not t.original_language, t.language or "", t.document_id))
    return TranslationsResult(
        document_id=document.id,
        title=document.title,
        language=document.language,
        concept=ConceptRef(
            id=concept.id,
            wikidata_qid=concept.wikidata_qid,
            label=concept.label,
            origin_lang=origin,
        ),
        is_original=origin is not None and document.language == origin,
        translations=found,
    )
