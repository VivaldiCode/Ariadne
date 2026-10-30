"""Estatísticas na API REST (ARCHITECTURE.md, secção 11.2): uso, documentos quentes, quem usa e a
cobertura do índice por fonte. Alimentam o módulo «Estatísticas» da app web."""

from typing import Annotated

from fastapi import APIRouter, Query

from ariadne import stats
from ariadne.api.deps import ServicesDep, SessionDep, UserDep
from ariadne.stats import Overview, PrincipalUsage, SourceCoverage, TopBy, TopDocument

router = APIRouter(prefix="/stats", tags=["statistics"])

Days = Annotated[
    int, Query(ge=1, le=stats.MAX_DAYS, description="Days to count, up to today (UTC).")
]


@router.get("/overview")
def get_overview(
    session: SessionDep, principal: UserDep, days: Days = stats.DEFAULT_DAYS
) -> Overview:
    """Queries per day and per level, level-3 answer rate, p50/p95 latency and index totals
    (documents, chunks, vectors, links); on large tables the totals are estimated. Heavy
    aggregations: `user` principals only."""
    return stats.overview(session, days)


@router.get("/top-documents")
def get_top_documents(
    services: ServicesDep,
    session: SessionDep,
    principal: UserDep,
    days: Days = stats.DEFAULT_DAYS,
    by: Annotated[TopBy, Query(description="heat (hottest), used or shown.")] = "heat",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[TopDocument]:
    """The hottest documents (current heat) or the most used/shown in the interval."""
    return stats.top_documents(
        session,
        days=days,
        by=by,
        limit=limit,
        half_life_days=services.settings.heat_half_life_days,
    )


@router.get("/principals")
def get_principals(
    session: SessionDep, principal: UserDep, days: Days = stats.DEFAULT_DAYS
) -> list[PrincipalUsage]:
    """Who uses it most: searches, calls, errors and each principal's last activity. `user`
    principals only."""
    return stats.principals_usage(session, days)


@router.get("/coverage")
def get_coverage(
    services: ServicesDep, session: SessionDep, principal: UserDep
) -> list[SourceCoverage]:
    """Index coverage per source: documents touched, indexed, with an article vector, imported
    into Wiki.js, and the curation status."""
    services.registry.sync_if_changed(session)
    counts = {s.id: s.article_count() for s in services.registry.active()}
    return stats.coverage(session, counts)
