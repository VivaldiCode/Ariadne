"""A fila de revisão humana da curadoria na API REST (STRUCTURE.md, secção 5). Só principais
`user`: um agente lê, não decide o que se publica."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ariadne.api.deps import AdminDep, SessionDep, UserDep
from ariadne.calls import tracked_call
from ariadne.curation import queue_view
from ariadne.curation.queue_view import (
    BatchReviewResult,
    CurationDetail,
    CurationPage,
    Decision,
)

router = APIRouter(tags=["curation"])


@router.get("/curation")
def list_curation(
    session: SessionDep,
    principal: UserDep,
    decision: Annotated[
        Decision | None, Query(description="published, pending_curator, needs_review, rejected.")
    ] = "needs_review",
    limit: Annotated[int, Query(ge=1, le=queue_view.MAX_LIST)] = 50,
    cursor: Annotated[
        str | None, Query(description="`next_cursor` from the previous page.")
    ] = None,
    batch_run_id: Annotated[
        int | None, Query(description="Only items curated by this external GPU run.")
    ] = None,
    from_batch: Annotated[
        bool, Query(description="Only items that came from some external GPU run.")
    ] = False,
) -> CurationPage:
    """The review queue (`needs_review` by default), newest items first."""
    arguments = {
        "decision": decision,
        "limit": limit,
        "cursor": cursor,
        "batch_run_id": batch_run_id,
        "from_batch": from_batch,
    }
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="curation_list",
        arguments=arguments,
    ) as call:
        page = queue_view.list_items(
            session,
            decision=decision,
            limit=limit,
            cursor=cursor,
            batch_run_id=batch_run_id,
            from_batch=from_batch,
        )
        call.result = {
            "count": len(page.items),
            "document_ids": [i.document_id for i in page.items],
            "more": page.next_cursor is not None,
        }
    return page


@router.get("/curation/item")
def get_curation(
    session: SessionDep,
    principal: UserDep,
    document_id: Annotated[str, Query(description="The document_id (it may contain '/').")],
) -> CurationDetail:
    """One item with the original (cleaned) and curated text side by side, flags and fidelity."""
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="curation_get",
        arguments={"document_id": document_id},
    ) as call:
        detail = queue_view.get_detail(session, document_id)
        call.result = {"document_id": document_id, "decision": detail.decision}
    return detail


class Review(BaseModel):
    document_id: str
    fingerprint: str  # a do item que viste (`GET /curation/item`): 409 se o texto mudou
    decision: Literal["approve", "reject"]
    note: str | None = None


@router.post("/curation/review")
def review_curation(session: SessionDep, principal: AdminDep, body: Review) -> CurationDetail:
    """Approve (the curated text, or the cleaned one if there is none, goes to Wiki.js) or reject an
    item; `reviewed_by` is set to your principal id. Requires the `fingerprint` of the item you
    saw (409 if the text changed). Rejecting an already published item removes the text from
    the page (withdrawal notice)."""
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="curation_review",
        arguments=body.model_dump(),
    ) as call:
        detail = queue_view.review_item(
            session,
            body.document_id,
            approve=body.decision == "approve",
            reviewer=str(principal.id),
            note=body.note,
            fingerprint=body.fingerprint,
        )
        session.commit()
        call.result = {"document_id": body.document_id, "decision": detail.decision}
    return detail


class ReviewItem(BaseModel):
    document_id: str
    fingerprint: str


class ReviewMany(BaseModel):
    items: list[ReviewItem] = Field(min_length=1, max_length=500)
    decision: Literal["approve", "reject"]
    note: str | None = None


@router.post("/curation/review-batch")
def review_curation_batch(
    session: SessionDep, principal: AdminDep, body: ReviewMany
) -> BatchReviewResult:
    """Batch review (the morning screen): approve or reject several items at once, each with the
    `fingerprint` you saw. Those that changed in the meantime land in `conflicts`, without
    undoing the others."""
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="curation_review_batch",
        arguments={"decision": body.decision, "count": len(body.items)},
    ) as call:
        result = queue_view.review_many(
            session,
            [(i.document_id, i.fingerprint) for i in body.items],
            approve=body.decision == "approve",
            reviewer=str(principal.id),
            note=body.note,
        )
        session.commit()
        call.result = {
            "done": len(result.done),
            "conflicts": len(result.conflicts),
            "missing": len(result.missing),
        }
    return result
