"""Estatísticas de uso e de cobertura do índice (ARCHITECTURE.md, secção 11.2; app web, módulo 3).

Tudo se calcula com agregações SQL sobre `query_log`, `query_result`, `tool_call` e as tabelas do
índice; nada é guardado à parte. Os dias são dias UTC (como o orçamento diário).
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from ariadne.db.models import (
    Chunk,
    ChunkVector,
    Curation,
    DocumentVector,
    Link,
    Principal,
    QueryLog,
    QueryResult,
    ToolCall,
)
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Source as SourceRow
from ariadne.heat import document_heat

MAX_DAYS = 365
DEFAULT_DAYS = 14


class DayPoint(BaseModel):
    day: date
    queries: int
    level1: int  # o resultado de topo veio do kiwix
    level2: int  # … do Wiki.js
    level3: int  # … do índice semântico
    empty: int  # sem resultados
    p50_ms: float | None
    p95_ms: float | None


class Totals(BaseModel):
    sources: int
    documents: int
    chunks: int
    chunk_vectors: int
    article_vectors: int
    links: dict[str, int]  # estado -> quantas


class Overview(BaseModel):
    days: int
    since: datetime
    queries: int
    level3_rate: float | None  # consultas servidas pelo nível 3 / consultas com resultados
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    series: list[DayPoint]  # um ponto por dia do intervalo, também os dias sem consultas
    totals: Totals


def window_start(days: int, now: datetime | None = None) -> datetime:
    """O início (meia-noite UTC) do primeiro dia de um intervalo de `days` dias que acaba hoje."""
    now = now or datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=max(1, days) - 1)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def overview(session: Session, days: int = DEFAULT_DAYS, now: datetime | None = None) -> Overview:
    days = max(1, min(days, MAX_DAYS))
    since = window_start(days, now)
    day = func.date(func.timezone("UTC", QueryLog.created_at))
    rows = session.execute(
        select(
            day.label("day"),
            func.count().label("queries"),
            func.count().filter(QueryLog.level_served == 1),
            func.count().filter(QueryLog.level_served == 2),
            func.count().filter(QueryLog.level_served == 3),
            func.count().filter(QueryLog.level_served.is_(None)),
            func.percentile_cont(0.5).within_group(QueryLog.latency_ms),
            func.percentile_cont(0.95).within_group(QueryLog.latency_ms),
        )
        .where(QueryLog.created_at >= since)
        .group_by(day)
    ).all()
    by_day = {r[0]: r for r in rows}
    series: list[DayPoint] = []
    for offset in range(days):
        current = (since + timedelta(days=offset)).date()
        r = by_day.get(current)
        series.append(
            DayPoint(
                day=current,
                queries=r[1] if r else 0,
                level1=r[2] if r else 0,
                level2=r[3] if r else 0,
                level3=r[4] if r else 0,
                empty=r[5] if r else 0,
                p50_ms=_round(r[6]) if r else None,
                p95_ms=_round(r[7]) if r else None,
            )
        )
    total, served3, with_results, p50, p95 = session.execute(
        select(
            func.count(),
            func.count().filter(QueryLog.level_served == 3),
            func.count().filter(QueryLog.level_served.is_not(None)),
            func.percentile_cont(0.5).within_group(QueryLog.latency_ms),
            func.percentile_cont(0.95).within_group(QueryLog.latency_ms),
        ).where(QueryLog.created_at >= since)
    ).one()
    return Overview(
        days=days,
        since=since,
        queries=total,
        level3_rate=round(served3 / with_results, 4) if with_results else None,
        latency_p50_ms=_round(p50),
        latency_p95_ms=_round(p95),
        series=series,
        totals=totals(session),
    )


# Acima disto os totais das tabelas grandes vêm da estimativa do Postgres (`pg_class.reltuples`,
# mantida pelo autovacuum) em vez de um `count(*)` que percorre a tabela a cada abertura da página.
ESTIMATE_ABOVE = 50_000


def _reltuples(session: Session, table: type) -> float:
    name = table.__tablename__  # type: ignore[attr-defined]
    estimate = session.scalar(
        text("SELECT reltuples FROM pg_class WHERE oid = to_regclass(:name)"), {"name": name}
    )
    return float(estimate) if estimate is not None else -1.0


def totals(session: Session) -> Totals:
    def count(table: type) -> int:
        """Exato nas tabelas pequenas ou nunca analisadas; estimado nas grandes."""
        estimate = _reltuples(session, table)
        if estimate >= ESTIMATE_ABOVE:
            return int(estimate)
        return int(session.scalar(select(func.count()).select_from(table)) or 0)

    links = dict(session.execute(select(Link.status, func.count()).group_by(Link.status)).all())
    return Totals(
        sources=count(SourceRow),
        documents=count(DocumentRow),
        chunks=count(Chunk),
        chunk_vectors=count(ChunkVector),
        article_vectors=count(DocumentVector),
        links={status: int(n) for status, n in links.items()},
    )


# --- documentos mais quentes / mais usados ---------------------------------------------------

TopBy = Literal["heat", "used", "shown"]


class TopDocument(BaseModel):
    document_id: str
    title: str
    source_id: str
    language: str | None
    heat: float  # o calor de agora (com o decaimento aplicado)
    shown: int  # vezes que apareceu em resultados, no intervalo
    used: int  # vezes que foi aberto/citado, no intervalo


def top_documents(
    session: Session, *, days: int, by: TopBy, limit: int, half_life_days: float
) -> list[TopDocument]:
    days = max(1, min(days, MAX_DAYS))
    limit = max(1, min(limit, 100))
    since = window_start(days)
    heat = document_heat(half_life_days)
    shown = func.count(QueryResult.query_id)
    used = func.count().filter(QueryResult.used.is_(True))
    usage = (
        select(
            QueryResult.document_id.label("document_id"), shown.label("shown"), used.label("used")
        )
        .join(QueryLog, QueryLog.id == QueryResult.query_id)
        .where(QueryLog.created_at >= since, QueryResult.document_id.is_not(None))
        .group_by(QueryResult.document_id)
        .subquery()
    )
    shown_col = func.coalesce(usage.c.shown, 0)
    used_col = func.coalesce(usage.c.used, 0)
    stmt = (
        select(DocumentRow, heat.label("heat"), shown_col, used_col)
        .outerjoin(usage, usage.c.document_id == DocumentRow.id)
        .limit(limit)
    )
    if by == "heat":
        stmt = stmt.where(DocumentRow.heat > 0).order_by(heat.desc(), DocumentRow.id)
    elif by == "used":
        stmt = stmt.where(used_col > 0).order_by(used_col.desc(), shown_col.desc(), DocumentRow.id)
    else:
        stmt = stmt.where(shown_col > 0).order_by(shown_col.desc(), DocumentRow.id)
    return [
        TopDocument(
            document_id=doc.id,
            title=doc.title,
            source_id=doc.source_id,
            language=doc.language,
            heat=round(float(h), 3),
            shown=int(s),
            used=int(u),
        )
        for doc, h, s, u in session.execute(stmt).all()
    ]


# --- quem mais usa -------------------------------------------------------------------------------


class PrincipalUsage(BaseModel):
    principal_id: int
    name: str
    kind: str
    role: str
    revoked: bool
    queries: int  # pesquisas
    tool_calls: int  # chamadas a ferramentas/endpoints (inclui as pesquisas)
    errors: int
    avg_latency_ms: float | None
    last_seen: datetime | None


def principals_usage(session: Session, days: int = DEFAULT_DAYS) -> list[PrincipalUsage]:
    days = max(1, min(days, MAX_DAYS))
    since = window_start(days)
    calls = (
        select(
            ToolCall.principal_id.label("pid"),
            func.count().label("calls"),
            func.count().filter(ToolCall.error.is_not(None)).label("errors"),
            func.avg(ToolCall.latency_ms).label("latency"),
            func.max(ToolCall.created_at).label("last"),
        )
        .where(ToolCall.created_at >= since)
        .group_by(ToolCall.principal_id)
        .subquery()
    )
    queries = (
        select(QueryLog.principal_id.label("pid"), func.count().label("n"))
        .where(QueryLog.created_at >= since)
        .group_by(QueryLog.principal_id)
        .subquery()
    )
    rows = session.execute(
        select(Principal, calls.c.calls, calls.c.errors, calls.c.latency, calls.c.last, queries.c.n)
        .outerjoin(calls, calls.c.pid == Principal.id)
        .outerjoin(queries, queries.c.pid == Principal.id)
        .order_by(
            func.coalesce(queries.c.n, 0).desc(),
            func.coalesce(calls.c.calls, 0).desc(),
            Principal.id,
        )
    ).all()
    return [
        PrincipalUsage(
            principal_id=p.id,
            name=p.name,
            kind=p.kind,
            role=p.role,
            revoked=p.api_key_hash is None,
            queries=int(n or 0),
            tool_calls=int(c or 0),
            errors=int(e or 0),
            avg_latency_ms=_round(lat),
            last_seen=last,
        )
        for p, c, e, lat, last, n in rows
    ]


# --- cobertura por fonte -----------------------------------------------------------------------


class SourceCoverage(BaseModel):
    source_id: str
    title: str
    language: str | None
    status: str
    articles: int | None  # artigos que a fonte declara (None se o ZIM não está aberto)
    documents: int  # documentos tocados: já vistos por uma pesquisa, vizinhança ou ligação
    indexed: int  # … com chunks no índice semântico
    chunks: int
    with_article_vector: int  # … com vetor de artigo (grafo entre fontes)
    imported: int  # … publicados no Wiki.js
    curation: dict[str, int]  # decisão -> quantos
    links: int  # ligações (aceites ou propostas) de documentos desta fonte


def coverage(session: Session, article_counts: Mapping[str, int]) -> list[SourceCoverage]:
    has_chunk = exists().where(Chunk.document_id == DocumentRow.id)
    has_vector = exists().where(DocumentVector.document_id == DocumentRow.id)
    docs = {
        r[0]: r
        for r in session.execute(
            select(
                DocumentRow.source_id,
                func.count(),
                func.count().filter(has_chunk),
                func.count().filter(has_vector),
                func.count().filter(DocumentRow.wiki_page_id.is_not(None)),
            ).group_by(DocumentRow.source_id)
        ).all()
    }
    chunks = dict(
        session.execute(
            select(DocumentRow.source_id, func.count())
            .select_from(Chunk)
            .join(DocumentRow, DocumentRow.id == Chunk.document_id)
            .group_by(DocumentRow.source_id)
        ).all()
    )
    curation: dict[str, dict[str, int]] = {}
    for source_id, decision, n in session.execute(
        select(DocumentRow.source_id, Curation.decision, func.count())
        .select_from(Curation)
        .join(DocumentRow, DocumentRow.id == Curation.document_id)
        .group_by(DocumentRow.source_id, Curation.decision)
    ):
        curation.setdefault(source_id, {})[decision] = int(n)
    links: dict[str, int] = {}
    for source_id, n in session.execute(
        select(DocumentRow.source_id, func.count())
        .select_from(Link)
        .join(DocumentRow, DocumentRow.id == Link.src_document)
        .where(Link.status != "rejected")
        .group_by(DocumentRow.source_id)
    ):
        links[source_id] = int(n)
    result = []
    for source in session.scalars(select(SourceRow).order_by(SourceRow.id)):
        d = docs.get(source.id)
        result.append(
            SourceCoverage(
                source_id=source.id,
                title=source.title,
                language=source.language,
                status=source.status,
                articles=article_counts.get(source.id),
                documents=int(d[1]) if d else 0,
                indexed=int(d[2]) if d else 0,
                chunks=int(chunks.get(source.id, 0)),
                with_article_vector=int(d[3]) if d else 0,
                imported=int(d[4]) if d else 0,
                curation=curation.get(source.id, {}),
                links=links.get(source.id, 0),
            )
        )
    return result
