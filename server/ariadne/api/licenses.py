"""Licenças (módulo «Licenças» da app): a classe de cada fonte e item e os bloqueios aplicados."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from ariadne import licenses
from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.db import models
from ariadne.documents import LibraryError, UnknownSource

router = APIRouter(tags=["licenses"])


class LicenseSource(BaseModel):
    id: str
    title: str
    kind: str
    status: str
    license_class: str
    documents: int
    private_documents: int  # documentos privados (todos, se a fonte é privada)


class GuardedOutput(BaseModel):
    """Uma saída para fora do NAS e como a classe de licença a guarda."""

    name: str
    mode: Literal["recusa", "filtra"]
    description: str


class BlockView(BaseModel):
    id: int
    created_at: datetime
    output: str
    action: Literal["blocked", "filtered"]
    items: int
    sample: list[dict[str, str]]


class LicenseOverview(BaseModel):
    sources: list[LicenseSource]
    private_sources: int
    open_sources: int
    private_documents: int
    open_documents: int
    outputs: list[GuardedOutput]
    blocks: list[BlockView]


class SourceMark(BaseModel):
    private: bool  # true: marca a fonte privada (persistente); false: tira a marca
    confirm: bool = False  # tirar a marca pede confirmação


class LicenseDocument(BaseModel):
    document_id: str
    title: str
    source_id: str
    content_type: str
    license_class: str
    because: str  # "private source" / "private document" / "open"


class LicenseDocumentsPage(BaseModel):
    total: int
    items: list[LicenseDocument]


# As saídas conhecidas hoje e como `ariadne.licenses` as guarda (ver ARCHITECTURE.md, secção 10).
OUTPUTS = [
    GuardedOutput(
        name="Vector export (ariadne vectors build --out)",
        mode="recusa",
        description="Refuses any private source (assert_exportable) before computing anything.",
    ),
    GuardedOutput(
        name="MemPalace mirror (ariadne mempalace sync)",
        mode="filtra",
        description="Leaves out the drawers of private documents (document_is_open).",
    ),
    GuardedOutput(
        name='Wiki.js pages ("Links" block)',
        mode="filtra",
        description="Does not point to private chapters, lessons or articles.",
    ),
    GuardedOutput(
        name="Releases and exports (Phase 12)",
        mode="recusa",
        description="They will call assert_exportable on all they carry; nothing private leaves.",
    ),
]


@router.get("/licenses")
def overview(session: SessionDep, principal: PrincipalDep) -> LicenseOverview:
    """The class of each source, how much is private and the blocks the outputs applied."""
    private = {
        key: value
        for key, value in session.execute(
            select(models.Document.source_id, func.count())
            .where(models.Document.license_class == "private")
            .group_by(models.Document.source_id)
        )
    }
    total = {
        key: value
        for key, value in session.execute(
            select(models.Document.source_id, func.count()).group_by(models.Document.source_id)
        )
    }
    sources = [
        LicenseSource(
            id=s.id,
            title=s.title,
            kind=s.kind,
            status=s.status,
            license_class=s.license_class,
            documents=int(total.get(s.id, 0)),
            private_documents=int(private.get(s.id, 0)),
        )
        for s in session.scalars(select(models.Source).order_by(models.Source.id))
    ]
    blocks = session.scalars(
        select(models.LicenseBlock).order_by(models.LicenseBlock.id.desc()).limit(20)
    ).all()
    private_documents = sum(private.values())
    return LicenseOverview(
        sources=sources,
        private_sources=sum(1 for s in sources if s.license_class == "private"),
        open_sources=sum(1 for s in sources if s.license_class == "open"),
        private_documents=private_documents,
        open_documents=sum(total.values()) - private_documents,
        outputs=OUTPUTS,
        blocks=[_block(b) for b in blocks],
    )


def _block(row: models.LicenseBlock) -> BlockView:
    return BlockView(
        id=row.id,
        created_at=row.created_at,
        output=row.output,
        action=row.action,
        items=row.items,
        sample=[{str(k): str(v) for k, v in item.items()} for item in row.sample],
    )


@router.get("/licenses/blocks")
def blocks(
    session: SessionDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BlockView]:
    """The blocks applied by the outputs, newest first."""
    rows = session.scalars(
        select(models.LicenseBlock).order_by(models.LicenseBlock.id.desc()).limit(limit)
    ).all()
    return [_block(r) for r in rows]


@router.get("/licenses/documents")
def documents(
    session: SessionDep,
    principal: PrincipalDep,
    source_id: Annotated[
        str | None, Query(description="Only the documents of this source.")
    ] = None,
    license_class: Annotated[
        Literal["open", "private"] | None, Query(description="Only this class.")
    ] = None,
    q: Annotated[str | None, Query(description="Part of the title.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LicenseDocumentsPage:
    """The documents with their license class (it comes from the source or the document itself)."""
    stmt = select(models.Document, models.Source.license_class).join(
        models.Source, models.Source.id == models.Document.source_id
    )
    if source_id:
        stmt = stmt.where(models.Document.source_id == source_id)
    if license_class:
        stmt = stmt.where(models.Document.license_class == license_class)
    if q:
        stmt = stmt.where(models.Document.title.ilike(f"%{q}%"))
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = session.execute(stmt.order_by(models.Document.id).limit(limit).offset(offset)).all()
    return LicenseDocumentsPage(
        total=total,
        items=[
            LicenseDocument(
                document_id=d.id,
                title=d.title,
                source_id=d.source_id,
                content_type=d.content_type,
                license_class=d.license_class,
                because=(
                    "private source"
                    if source_class == "private"
                    else "private document"
                    if d.license_class == "private"
                    else "open"
                ),
            )
            for d, source_class in rows
        ],
    )


@router.put("/licenses/sources/{source_id}")
def mark_source(
    source_id: str,
    body: SourceMark,
    session: SessionDep,
    services: ServicesDep,
    principal: AdminDep,
) -> LicenseSource:
    """Mark a source (a ZIM, for example) as private, persistently: it survives source syncing
    and takes its documents with it. Removing the mark (`private=false`) requires `confirm`:
    it asserts the source can be redistributed."""
    if session.get(models.Source, source_id) is None:
        raise UnknownSource(f"Unknown source: {source_id!r} (see GET /api/licenses).")
    if body.private:
        licenses.mark_source_private(session, source_id)
    else:
        if not body.confirm:
            raise LibraryError(
                "Removing the mark returns the source to the class it declares (open, for ZIMs): "
                "repeat with confirm=true if you have the right to redistribute it."
            )
        licenses.clear_source_mark(session, source_id)
    session.commit()
    services.registry.sync(session)
    session.expire_all()
    source = session.get(models.Source, source_id)
    assert source is not None
    docs = int(
        session.scalar(
            select(func.count())
            .select_from(models.Document)
            .where(models.Document.source_id == source_id)
        )
        or 0
    )
    private = int(
        session.scalar(
            select(func.count())
            .select_from(models.Document)
            .where(
                models.Document.source_id == source_id, models.Document.license_class == "private"
            )
        )
        or 0
    )
    return LicenseSource(
        id=source.id,
        title=source.title,
        kind=source.kind,
        status=source.status,
        license_class=source.license_class,
        documents=docs,
        private_documents=private,
    )
