"""Catálogo do Kiwix no dashboard (Fase 12; STRUCTURE.md, 4 e 10.2).

`GET /api/catalog` navega o catálogo OPDS (qualquer principal); descarregar, cancelar, arquivar e
apagar ZIM é só dos administradores. A descarga é o job `catalog_download` do worker `catalog`; o
progresso vê-se em `GET /api/catalog/downloads`.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.db.models import Job
from ariadne.queue import FAILED
from ariadne.workers.catalog import KIND, active_downloads, enqueue_download, request_cancel
from ariadne.zimcatalog.library import (
    RemovalResult,
    SourceNotRemovable,
    installed_index,
    remove_zim,
    restore_zim,
)
from ariadne.zimcatalog.opds import CatalogEntry, CatalogFilters, EntryNotFound

router = APIRouter(tags=["catalog"])


class DownloadView(BaseModel):
    job_id: int
    book: str
    name: str
    title: str
    status: str  # pending | running | done | failed
    phase: str  # queued | resolving | downloading | verifying | registering | serving | done | …
    bytes: int
    total: int | None
    filename: str | None
    source_id: str | None
    served: bool | None  # o kiwix-serve já serve o ZIM?
    error: str | None
    requested_by: str | None
    created_at: str
    finished_at: str | None


class CatalogItem(CatalogEntry):
    installed: bool = False  # este ficheiro (esta versão) já está na pasta
    installed_source: str | None = None
    installed_version: str | None = None  # outra versão do mesmo livro
    update_available: bool = False
    archived: bool = False
    download: DownloadView | None = None


class CatalogView(BaseModel):
    total: int
    start: int
    count: int
    items: list[CatalogItem]


def download_view(job: Job, *, detail: bool = True) -> DownloadView:
    """`detail=False` (agentes) esconde o erro e quem pediu: só pessoas veem esses pormenores."""
    p: dict[str, Any] = job.payload
    total = p.get("total")
    return DownloadView(
        job_id=job.id,
        book=str(p.get("book", "")),
        name=str(p.get("name", "")),
        title=str(p.get("title", "")),
        status=job.status,
        phase=str(p.get("phase", "queued")) if job.status != FAILED else "failed",
        bytes=int(p.get("bytes") or 0),
        total=int(total) if isinstance(total, int) else None,
        filename=p.get("filename"),
        source_id=p.get("source_id"),
        served=p.get("served"),
        error=(job.last_error or p.get("error")) if detail else None,
        requested_by=p.get("requested_by") if detail else None,
        created_at=job.created_at.isoformat(),
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
    )


def _annotate(
    session: Session, entries: list[CatalogEntry], *, detail: bool = True
) -> list[CatalogItem]:
    sources = installed_index(session)
    by_stem = {str(s.meta.get("kiwix_book")): s for s in sources if s.meta.get("kiwix_book")}
    by_name: dict[tuple[str, str], Any] = {}
    for s in sources:
        by_name[(str(s.meta.get("zim_name", "")), str(s.meta.get("flavour", "")))] = s
    downloads: dict[str, Job] = {}
    for recent in active_downloads(session):  # do mais recente para o mais antigo
        downloads.setdefault(str(recent.payload.get("book")), recent)
    items: list[CatalogItem] = []
    for entry in entries:
        item = CatalogItem(**entry.model_dump())
        same = by_stem.get(entry.stem or "")
        other = by_name.get((entry.name, entry.flavour))
        if same is not None:
            item.installed = same.status == "active"
            item.archived = same.status == "archived"
            item.installed_source = same.id
            item.installed_version = same.version
        elif other is not None:
            item.installed_source = other.id
            item.installed_version = other.version
            item.archived = other.status == "archived"
            item.update_available = other.status == "active" and other.version < entry.updated[:7]
        job = downloads.get(entry.id)
        if job is not None:
            item.download = download_view(job, detail=detail)
        items.append(item)
    return items


@router.get("/catalog")
def get_catalog(
    services: ServicesDep,
    session: SessionDep,
    principal: PrincipalDep,
    q: Annotated[str | None, Query(description="Text to search for in the catalog.")] = None,
    lang: Annotated[str | None, Query(description="ISO 639-3 language (e.g. por, eng).")] = None,
    category: Annotated[str | None, Query(description="Category (e.g. wikipedia).")] = None,
    start: Annotated[int, Query(ge=0)] = 0,
    count: Annotated[int, Query(ge=1, le=100)] = 24,
) -> CatalogView:
    """The Kiwix OPDS catalog (`KIWIX_CATALOG_URL`), with search, language, category and
    pagination; each book says whether it is already installed or a newer version exists."""
    page = services.get_catalog().search(
        q=q, lang=lang, category=category, start=start, count=count
    )
    return CatalogView(
        total=page.total,
        start=page.start,
        count=page.count,
        items=_annotate(session, page.items, detail=principal.kind == "user"),
    )


@router.get("/catalog/filters")
def get_catalog_filters(services: ServicesDep, principal: PrincipalDep) -> CatalogFilters:
    """The catalog languages and categories (for the filters)."""
    return services.get_catalog().filters()


@router.get("/catalog/downloads")
def get_downloads(session: SessionDep, principal: PrincipalDep) -> list[DownloadView]:
    """Downloads waiting, running or finished less than a day ago, with their progress."""
    detail = principal.kind == "user"
    return [download_view(job, detail=detail) for job in active_downloads(session)]


@router.post("/catalog/{book}/download", status_code=status.HTTP_202_ACCEPTED)
def start_download(
    services: ServicesDep,
    session: SessionDep,
    admin: AdminDep,
    book: str,
    allow_unverified: Annotated[
        bool,
        Query(
            description="Download even without the catalog sha-256 (third-party mirrors): "
            "an explicit admin decision."
        ),
    ] = False,
) -> DownloadView:
    """Queue the download of a catalog book into ZIM_DIR (admins only). The ZIM is registered
    as a source and kiwix-serve starts serving it on its own."""
    try:
        entry = services.get_catalog().entry(book)
    except EntryNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not entry.download_url or not entry.stem:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="The entry has no download.")
    installed = {s.meta.get("kiwix_book"): s for s in installed_index(session)}
    if entry.stem in installed and installed[entry.stem].status == "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"{entry.stem}.zim is already installed."
        )
    job_id = enqueue_download(session, entry, admin.name, allow_unverified=allow_unverified)
    session.commit()
    job = session.get(Job, job_id)
    assert job is not None and job.kind == KIND
    return download_view(job)


@router.delete("/catalog/downloads/{job_id}")
def cancel_download(session: SessionDep, admin: AdminDep, job_id: int) -> dict[str, str]:
    """Cancel a download (a running one stops at the next block and leaves the `.part` to
    resume)."""
    job = session.scalar(select(Job).where(Job.id == job_id, Job.kind == KIND).with_for_update())
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown download.")
    outcome = request_cancel(session, job)
    session.commit()
    return {"result": outcome}


@router.delete("/sources/{source_id}")
def delete_source(
    services: ServicesDep,
    session: SessionDep,
    admin: AdminDep,
    source_id: str,
    delete_file: Annotated[
        bool, Query(description="Really delete the file (requires `confirm`).")
    ] = False,
    confirm: Annotated[str | None, Query(description="The source id, repeated.")] = None,
) -> RemovalResult:
    """Archive a ZIM (the file moves to `.archive/` and kiwix stops serving it). It deletes the
    file only with `delete_file=true&confirm=<source id>`. Admins only."""
    try:
        return remove_zim(services, session, source_id, delete_file=delete_file, confirm=confirm)
    except SourceNotRemovable as exc:
        code = (
            status.HTTP_404_NOT_FOUND if "does not exist" in str(exc) else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, detail=str(exc)) from exc


@router.post("/sources/{source_id}/restore")
def restore_source(
    services: ServicesDep, session: SessionDep, admin: AdminDep, source_id: str
) -> RemovalResult:
    """Put an archived ZIM back in the folder (admins only)."""
    try:
        return restore_zim(services, session, source_id)
    except SourceNotRemovable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
