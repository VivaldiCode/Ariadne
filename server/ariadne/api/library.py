"""API REST da biblioteca (ARCHITECTURE.md, secção 11.2): as mesmas operações do MCP."""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import Field

from ariadne.api.deps import PrincipalDep, ServicesDep, SessionDep
from ariadne.calls import tracked_call
from ariadne.catalog import SourceInfo, list_sources
from ariadne.documents import DEFAULT_MAX_CHARS, DocumentView, read_document
from ariadne.rooms import DEFAULT_LIST_LIMIT, RoomInfo, list_rooms
from ariadne.search import DEFAULT_K, MAX_ROOM_ID, SearchResult
from ariadne.search import search as run_search

router = APIRouter(tags=["library"])


@router.get("/search")
def search(
    services: ServicesDep,
    session: SessionDep,
    principal: PrincipalDep,
    q: Annotated[str, Query(min_length=1, description="Search terms.")],
    lang: Annotated[
        str | None, Query(description="Only sources in this language (e.g. pt).")
    ] = None,
    sources: Annotated[
        list[str] | None,
        Query(
            description="Only these sources (ids); repeat the parameter or separate with commas."
        ),
    ] = None,
    k: Annotated[int, Query(description="Maximum results (1–50).")] = DEFAULT_K,
    wings: Annotated[
        list[str] | None,
        Query(description="Only these wings (see `wing` in /api/sources); repeat or use commas."),
    ] = None,
    rooms: Annotated[
        list[Annotated[int, Field(ge=1, le=MAX_ROOM_ID)]] | None,
        Query(description="Only documents in these rooms (ids from /api/rooms)."),
    ] = None,
) -> SearchResult:
    """Search the library: level 3 (semantic) and, depending on confidence, kiwix (level 1)."""
    chosen = [s.strip() for item in sources or [] for s in item.split(",") if s.strip()] or None
    chosen_wings = [w.strip() for item in wings or [] for w in item.split(",") if w.strip()] or None
    arguments: dict[str, object] = {"query": q, "lang": lang, "sources": chosen, "k": k}
    if chosen_wings:
        arguments["wings"] = chosen_wings
    if rooms:
        arguments["rooms"] = rooms
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="search",
        arguments=arguments,
    ) as call:
        result = run_search(
            services,
            session,
            principal.id,
            q,
            lang=lang,
            sources=chosen,
            k=k,
            wings=chosen_wings,
            rooms=rooms,
        )
        call.query_id = result.query_id
        call.result = result.call_summary()
    return result


# ATENÇÃO: `{document_id:path}` apanha tudo, '/' incluídos (um id que acabe em '/links' também):
# por isso os vizinhos vão em `GET /api/neighbors?document_id=`, e não em `/documents/{id}/links`.
@router.get("/documents/{document_id:path}")
def get_document(
    services: ServicesDep,
    session: SessionDep,
    principal: PrincipalDep,
    document_id: str,
    section: Annotated[str | None, Query(description="Title of a section.")] = None,
    max_chars: Annotated[int, Query(description="Cut the text to this length.")] = (
        DEFAULT_MAX_CHARS
    ),
    offset: Annotated[
        int, Query(ge=0, description="Skip the first characters (the position of a citation).")
    ] = 0,
) -> DocumentView:
    """Read a document: clean text, sections and links (same as the `read` tool)."""
    arguments = {
        "document_id": document_id,
        "section": section,
        "max_chars": max_chars,
        **({"offset": offset} if offset else {}),
    }
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="read",
        arguments=arguments,
    ) as call:
        view = read_document(
            services,
            session,
            principal.id,
            document_id,
            section=section,
            max_chars=max_chars,
            offset=offset,
        )
        call.result = view.call_summary()
    return view


@router.get("/rooms")
def get_rooms(
    session: SessionDep,
    principal: PrincipalDep,
    wing: Annotated[str | None, Query(description="Only the rooms of this wing.")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Maximum rooms.")] = DEFAULT_LIST_LIMIT,
) -> list[RoomInfo]:
    """The rooms (themes) of a wing or of all wings, largest first."""
    return list_rooms(session, wing=wing, limit=limit)


@router.get("/sources")
def get_sources(
    services: ServicesDep, session: SessionDep, principal: PrincipalDep
) -> list[SourceInfo]:
    """Catalog of the available sources."""
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="sources",
        arguments={},
    ) as call:
        found = list_sources(services, session)
        call.result = {"count": len(found), "source_ids": [s.id for s in found]}
    return found
