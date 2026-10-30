"""Coleções a pré-aquecer (app web, módulo «Ligações»): uma lista de caminhos ou títulos de uma
fonte que se põe na fila de indexação, para o nível 3 já os ter quando alguém os pesquisar."""

import math
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ariadne import router as ariadne_router
from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.calls import tracked_call
from ariadne.db.models import Collection
from ariadne.documents import UnknownSource
from ariadne.textutil import clean_text
from ariadne.warm import warm_entries

router = APIRouter(prefix="/collections", tags=["collections"])

MAX_ENTRIES = 2000  # a resolução é síncrona (lê cada artigo)
MAX_ENTRY_CHARS = 300  # um caminho ou título
DEFAULT_PRIORITY = 0.1  # como `ariadne index enqueue`: o uso real (≥ 0,3) passa à frente
MAX_PER_HOUR = 20  # coleções por hora e principal


class CollectionView(BaseModel):
    id: int
    name: str
    source_id: str
    entries: int  # quantas entradas foram pedidas
    queued: int  # postas na fila de indexação
    known: int  # já indexadas, ou com falha recente
    ignored: list[str]  # entradas que não são artigos da fonte
    priority: float
    created_by: str
    created_at: datetime


class CollectionDetail(CollectionView):
    paths: list[str]  # as entradas, como foram escritas


class NewCollection(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    source_id: str = Field(description="The source id (see GET /api/sources).")
    paths: Annotated[
        list[Annotated[str, Field(max_length=MAX_ENTRY_CHARS)]],
        Field(
            min_length=1,
            max_length=MAX_ENTRIES,
            description=f"Paths or titles, one per item (up to {MAX_ENTRY_CHARS} characters; "
            f"at most {MAX_ENTRIES} items).",
        ),
    ]
    priority: Annotated[float, Field(ge=0, le=1)] = DEFAULT_PRIORITY


def _view(row: Collection) -> CollectionView:
    return CollectionView(
        id=row.id,
        name=row.name,
        source_id=row.source_id,
        entries=len(row.entries),
        queued=row.queued,
        known=row.known,
        ignored=row.ignored,
        priority=row.priority,
        created_by=row.created_by,
        created_at=row.created_at,
    )


@router.get("")
def list_collections(
    session: SessionDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CollectionView]:
    """The collections already requested, newest first."""
    rows = session.scalars(select(Collection).order_by(Collection.id.desc()).limit(limit))
    return [_view(r) for r in rows]


@router.get("/{collection_id}")
def get_collection(
    session: SessionDep, principal: PrincipalDep, collection_id: int
) -> CollectionDetail:
    row = session.get(Collection, collection_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Collection {collection_id} does not exist."
        )
    return CollectionDetail(**_view(row).model_dump(), paths=row.entries)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_collection(
    services: ServicesDep, session: SessionDep, principal: AdminDep, body: NewCollection
) -> CollectionView:
    """Queue the listed articles for indexing (same as `ariadne index enqueue --paths`): resolve
    each path or title in the source, skip what is already indexed and store the collection.
    Indexing respects the daily budget. Admins only."""
    if not math.isfinite(body.priority):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The priority must be finite.")
    recent = session.scalar(
        select(func.count()).where(
            Collection.principal_id == principal.id,
            Collection.created_at > func.now() - timedelta(hours=1),
        )
    )
    if (recent or 0) >= MAX_PER_HOUR:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"At most {MAX_PER_HOUR} collections per hour; try again later.",
        )
    entries = [clean_text(e).strip() for e in body.paths]
    entries = [e for e in entries if e and not e.startswith("#")]
    if not entries:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The list has no paths.")
    arguments = {"name": body.name, "source_id": body.source_id, "entries": len(entries)}
    principal_name, principal_id = principal.name, principal.id
    with tracked_call(
        session,
        principal_id=principal_id,
        session_id=None,
        tool="collection_create",
        arguments=arguments,
    ) as call:
        services.registry.sync_if_changed(session)
        source = services.registry.get(body.source_id)
        if source is None:
            names = ", ".join(s.id for s in services.registry.active()) or "(none)"
            raise UnknownSource(f"Unknown source: {body.source_id!r}. Available: {names}.")
        report = warm_entries(
            session,
            source,
            entries,
            priority=body.priority,
            store=services.store,
            model_id=ariadne_router.current_model_id(services),
        )
        row = Collection(
            name=clean_text(body.name).strip(),
            source_id=source.id,
            entries=entries,
            priority=body.priority,
            queued=report.queued,
            known=report.known,
            ignored=report.ignored,
            created_by=principal_name,
            principal_id=principal_id,
        )
        session.add(row)
        session.flush()
        view = _view(row)
        session.commit()
        call.result = {"collection_id": view.id, "queued": report.queued}
    return view
