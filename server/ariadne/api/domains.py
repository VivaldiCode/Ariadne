"""A árvore de domínios do Wiki.js e a colocação dos documentos (docs/WIKI-IMPORT.md, secção 6).
Só principais `user`, como a curadoria: uma pessoa decide onde um documento fica."""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

from ariadne.api.deps import AdminDep, SessionDep, UserDep
from ariadne.calls import tracked_call
from ariadne.wiki import domains

router = APIRouter(tags=["domains"])


class DomainNode(BaseModel):
    id: str
    parent_id: str | None
    position: int
    names: dict[str, str]
    slugs: dict[str, str]
    descriptions: dict[str, str]
    active: bool
    documents: int = Field(description="Documents placed directly in this domain.")
    documents_total: int = Field(description="Those of the whole subtree, starting at this node.")
    needs_review: int = Field(description="Placements in this domain waiting for review.")
    children: list["DomainNode"]


class DomainTree(BaseModel):
    domains: list[DomainNode]


@router.get("/domains")
def list_domains(session: SessionDep, principal: UserDep) -> DomainTree:
    """The domain tree (names, *slugs* and descriptions in pt and en) with the count of documents
    placed at each node. Nodes removed from the YAML come with `active: false`."""
    with tracked_call(
        session, principal_id=principal.id, session_id=None, tool="domains_list", arguments={}
    ) as call:
        roots = domains.tree(session)
        call.result = {"roots": len(roots)}
    return DomainTree.model_validate({"domains": roots})


class Assignment(BaseModel):
    domain_id: str = Field(description="The domain id, for example `transportes/automoveis`.")
    note: str | None = Field(default=None, description="Why it was chosen (kept in the signals).")


class AssignmentOut(BaseModel):
    document_id: str
    domain_id: str
    confidence: float
    method: str
    needs_review: bool
    signals: dict[str, Any]


@router.put("/domain-assignments/{document_id:path}")
def assign_domain(
    session: SessionDep,
    principal: AdminDep,
    document_id: Annotated[str, Path(description="The document_id (it contains '/').")],
    body: Assignment,
) -> AssignmentOut:
    """Place the document in a domain by hand: `method='human'`, confidence 1, no review. The
    automatic passes (prior, vectors, LLM) never override a human placement."""
    arguments = {"document_id": document_id, **body.model_dump()}
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="domain_assign",
        arguments=arguments,
    ) as call:
        try:
            placement = domains.assign_human(
                session,
                document_id=document_id,
                domain_id=body.domain_id,
                reviewer=str(principal.id),
                note=body.note,
            )
        except domains.DomainError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        session.commit()
        call.result = {"document_id": document_id, "domain_id": body.domain_id}
    return AssignmentOut(
        document_id=document_id,
        domain_id=placement.domain_id,
        confidence=placement.confidence,
        method=placement.method,
        needs_review=placement.needs_review,
        signals=placement.signals,
    )
