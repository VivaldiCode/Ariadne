"""A fila de revisão humana da curadoria (STRUCTURE.md, secção 5, passo 10): listar, ver o original
e o curado lado a lado, aprovar ou rejeitar."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ariadne.curation import pipeline
from ariadne.db.models import Curation
from ariadne.db.models import Document as DocumentRow
from ariadne.documents import LibraryError
from ariadne.wiki.refresh import enqueue_import

MAX_LIST = 200
APPROVE_PRIORITY = 0.8  # um humano decidiu: publica-se à frente do resto

Decision = Literal["published", "pending_curator", "needs_review", "rejected"]


class CurationNotFound(LibraryError):
    pass


class CurationItem(BaseModel):
    document_id: str
    fingerprint: str  # o texto que se viu: aprovar/rejeitar exige a mesma
    title: str
    source_id: str
    language: str | None
    decision: str
    reason: str | None
    flags: list[str]
    sampled: bool
    curator_model: str | None
    fidelity_ok: bool | None
    reviewed_by: str | None
    batch_run_id: int | None  # a corrida de GPU externa que produziu o curado
    updated_at: datetime


class CurationPage(BaseModel):
    items: list[CurationItem]
    next_cursor: str | None


class CurationDetail(CurationItem):
    """Os dois textos lado a lado: `original_md` (limpeza determinística) e `curated_md`."""

    source_version: str
    flag_details: list[dict[str, Any]]
    fidelity: dict[str, Any] | None
    original_md: str
    curated_md: str | None
    published_md: str | None
    archived: bool


def _item(row: Curation, document: DocumentRow) -> dict[str, Any]:
    return {
        "document_id": row.document_id,
        "fingerprint": row.fingerprint,
        "title": document.title,
        "source_id": document.source_id,
        "language": document.language,
        "decision": row.decision,
        "reason": row.reason,
        "flags": sorted({str(f.get("code")) for f in row.flags}),
        "sampled": row.sampled,
        "curator_model": row.curator_model,
        "fidelity_ok": None if row.fidelity is None else row.fidelity.get("ok"),
        "reviewed_by": row.reviewed_by,
        "batch_run_id": row.batch_run_id,
        "updated_at": row.updated_at,
    }


def list_items(
    session: Session,
    *,
    decision: Decision | None,
    limit: int,
    cursor: str | None,
    batch_run_id: int | None = None,
    from_batch: bool = False,
) -> CurationPage:
    """Os itens por estado, dos mais recentes para os mais antigos (keyset por data e id)."""
    conditions = []
    if decision is not None:
        conditions.append(Curation.decision == decision)
    if batch_run_id is not None:
        conditions.append(Curation.batch_run_id == batch_run_id)
    elif from_batch:
        conditions.append(Curation.batch_run_id.is_not(None))
    if cursor is not None:
        stamp, _, last_id = cursor.partition("|")
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise LibraryError(f"Invalid cursor: {cursor!r}.") from exc
        conditions.append(
            or_(
                Curation.updated_at < moment,
                and_(Curation.updated_at == moment, Curation.document_id > last_id),
            )
        )
    limit = max(1, min(limit, MAX_LIST))
    rows = session.execute(
        select(Curation, DocumentRow)
        .join(DocumentRow, DocumentRow.id == Curation.document_id)
        .where(*conditions)
        .order_by(Curation.updated_at.desc(), Curation.document_id)
        .limit(limit + 1)
    ).all()
    page = rows[:limit]
    items = [CurationItem(**_item(c, d)) for c, d in page]
    more = len(rows) > limit
    last = page[-1][0] if page else None
    next_cursor = f"{last.updated_at.isoformat()}|{last.document_id}" if more and last else None
    return CurationPage(items=items, next_cursor=next_cursor)


def get_detail(session: Session, document_id: str) -> CurationDetail:
    row = session.get(Curation, document_id)
    document = session.get(DocumentRow, document_id)
    if row is None or document is None:
        raise CurationNotFound(f"Document {document_id!r} has no curation.")
    return CurationDetail(
        **_item(row, document),
        source_version=row.source_version,
        flag_details=row.flags,
        fidelity=row.fidelity,
        original_md=row.original_md,
        curated_md=row.curated_md,
        published_md=row.published_md,
        archived=row.archived_at is not None,
    )


def review_item(
    session: Session,
    document_id: str,
    *,
    approve: bool,
    reviewer: str,
    note: str | None,
    fingerprint: str,
) -> CurationDetail:
    """Aprova (publica o curado, ou o limpo se não há curado) ou rejeita, desde que o texto seja o
    que o revisor viu (`CurationConflict`, 409, se mudou entretanto). Rejeitar retira também o que
    estava publicado (a página fica com um aviso de retirada, nunca se apaga). Não faz commit."""
    row = pipeline.review(session, document_id, approve, reviewer, note, fingerprint)
    if row is None:
        raise CurationNotFound(f"Document {document_id!r} has no curation.")
    if approve or row.published_md is not None:
        enqueue_import(session, document_id, APPROVE_PRIORITY, force=True)
    session.flush()
    return get_detail(session, document_id)


class BatchReviewResult(BaseModel):
    done: list[str]
    conflicts: list[str]  # o texto mudou desde que o viste
    missing: list[str]  # sem curadoria


def review_many(
    session: Session,
    items: list[tuple[str, str]],
    *,
    approve: bool,
    reviewer: str,
    note: str | None,
) -> BatchReviewResult:
    """A revisão em lote: aprova ou rejeita vários itens `(document_id, fingerprint)` de uma vez.
    Cada um é independente (um conflito não desfaz os outros). Não faz commit."""
    result = BatchReviewResult(done=[], conflicts=[], missing=[])
    for document_id, fingerprint in items:
        try:
            with session.begin_nested():
                review_item(
                    session,
                    document_id,
                    approve=approve,
                    reviewer=reviewer,
                    note=note,
                    fingerprint=fingerprint,
                )
        except pipeline.CurationConflict:
            result.conflicts.append(document_id)
        except CurationNotFound:
            result.missing.append(document_id)
        else:
            result.done.append(document_id)
    return result
