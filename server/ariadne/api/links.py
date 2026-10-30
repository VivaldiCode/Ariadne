"""Ligações entre documentos na API REST (ARCHITECTURE.md, secções 7 e 11.2)."""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel

from ariadne import graph
from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.calls import tracked_call
from ariadne.graph import LinksPage, LinkView, NeighborsResult, Status

router = APIRouter(tags=["links"])


@router.get("/neighbors")
def get_neighbors(
    services: ServicesDep,
    session: SessionDep,
    principal: PrincipalDep,
    document_id: Annotated[str, Query(description="The document_id (it may contain '/').")],
    kinds: Annotated[
        list[str] | None,
        Query(description="Only these types (repeat the parameter or separate with commas)."),
    ] = None,
    k: Annotated[int, Query(description="Maximum neighbors (1–100).")] = graph.DEFAULT_K,
) -> NeighborsResult:
    """The documents linked to this one (same as the MCP `neighbors` tool). The id goes in a
    parameter, not in the path: an id ending in '/links' would be ambiguous."""
    chosen = [s.strip() for item in kinds or [] for s in item.split(",") if s.strip()] or None
    arguments = {"document_id": document_id, "kinds": chosen, "k": k}
    with tracked_call(
        session, principal_id=principal.id, session_id=None, tool="neighbors", arguments=arguments
    ) as call:
        result = graph.neighbors(services, session, document_id, kinds=chosen, k=k)
        call.result = result.call_summary()
    return result


@router.get("/links")
def list_links(
    session: SessionDep,
    principal: PrincipalDep,
    link_status: Annotated[
        Status | None, Query(alias="status", description="proposed, accepted or rejected.")
    ] = None,
    kind: Annotated[str | None, Query(description="Link type (e.g. vector).")] = None,
    document_id: Annotated[
        str | None, Query(description="Only the links of this document (in both directions).")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=graph.MAX_LIST)] = 50,
    cursor: Annotated[
        str | None, Query(description="`next_cursor` from the previous page (keyset).")
    ] = None,
) -> LinksPage:
    """The curation queue: links by status (`?status=proposed`), type and document, highest
    scored first, paginated by cursor."""
    arguments = {
        "status": link_status,
        "kind": kind,
        "document_id": document_id,
        "limit": limit,
        "cursor": cursor,
    }
    with tracked_call(
        session, principal_id=principal.id, session_id=None, tool="links", arguments=arguments
    ) as call:
        page = graph.list_links(
            session,
            status=link_status,
            kind=kind,
            document_id=document_id,
            limit=limit,
            cursor=cursor,
        )
        call.result = {
            "count": len(page.items),
            "link_ids": [link.id for link in page.items],
            "more": page.next_cursor is not None,
        }
    return page


class NewLink(BaseModel):
    src_document: str
    dst_document: str


@router.post("/links")
def create_link(
    session: SessionDep,
    principal: AdminDep,
    body: NewLink,
    response: Response,
) -> LinkView:
    """Link two already seen documents by hand (`manual`, accepted, `created_by` = your name). The
    link works in both directions. `user` principals only."""
    arguments = body.model_dump()
    with tracked_call(
        session, principal_id=principal.id, session_id=None, tool="link_create", arguments=arguments
    ) as call:
        view, created = graph.create_manual_link(
            session, principal.name, str(principal.id), body.src_document, body.dst_document
        )
        session.commit()
        call.result = {"link_id": view.id, "created": created}
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return view


class Review(BaseModel):
    status: Status


@router.post("/links/{link_id}")
def review_link(session: SessionDep, principal: AdminDep, link_id: int, body: Review) -> LinkView:
    """Accept (`accepted`) or reject (`rejected`) a link; `reviewed_by` is set to your principal
    id. A rejected link stops appearing in `neighbors` and the linker does not recreate it.
    `user` principals only."""
    arguments = {"link_id": link_id, "status": body.status}
    with tracked_call(
        session, principal_id=principal.id, session_id=None, tool="link_review", arguments=arguments
    ) as call:
        view = graph.review_link(session, link_id, str(principal.id), body.status)
        session.commit()
        call.result = {"link_id": link_id, "status": body.status}
    return view
