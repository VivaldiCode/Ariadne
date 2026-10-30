"""Uso e quota dos clientes externos (chaves `mcp`): o que o portal comercial consulta.

Conta-se sobre `tool_call` (cada chamada a uma ferramenta MCP), por dia UTC e por ferramenta. A
quota mensal conta as chamadas do mês UTC corrente que não falharam; uma chamada com erro não
gasta quota (o cliente não leva culpa de uma falha do servidor).
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariadne.db.models import Principal, ToolCall
from ariadne.stats import MAX_DAYS, window_start

DEFAULT_USAGE_DAYS = 30


class DayUsage(BaseModel):
    day: date
    tool_calls: int
    errors: int
    searches: int
    empty_searches: int  # pesquisas sem resultados (cobertura fraca para este cliente)


class ToolUsage(BaseModel):
    tool: str
    calls: int
    errors: int


class MonthUsage(BaseModel):
    period_start: datetime
    resets_at: datetime  # início do mês seguinte (UTC): quando a quota volta a zero
    used: int
    quota: int | None  # None: sem quota
    remaining: int | None


class ClientUsage(BaseModel):
    principal_id: int
    name: str
    scope: str
    suspended: bool
    revoked: bool
    days: int
    since: datetime
    tool_calls: int
    errors: int
    searches: int
    empty_searches: int
    last_used_at: datetime | None  # a última chamada de sempre, não só do intervalo
    month: MonthUsage
    daily: list[DayUsage]  # um ponto por dia do intervalo, com zeros
    by_tool: list[ToolUsage]  # as mais usadas primeiro


def month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """O início do mês UTC de `now` e o início do seguinte."""
    now = now or datetime.now(UTC)
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (start + timedelta(days=32)).replace(day=1)
    return start, following


def month_used(session: Session, principal_id: int, now: datetime | None = None) -> int:
    """Chamadas do principal neste mês UTC que contam para a quota."""
    start, _ = month_bounds(now)
    return int(
        session.scalar(
            select(func.count()).where(
                ToolCall.principal_id == principal_id,
                ToolCall.created_at >= start,
                ToolCall.error.is_(None),
            )
        )
        or 0
    )


def quota_exceeded(
    session: Session, principal_id: int, quota: int | None, now: datetime | None = None
) -> bool:
    return quota is not None and month_used(session, principal_id, now) >= quota


def usage(
    session: Session,
    principals: Sequence[Principal],
    days: int = DEFAULT_USAGE_DAYS,
    now: datetime | None = None,
) -> list[ClientUsage]:
    """O uso de cada principal pedido, com três consultas seja qual for o número deles."""
    days = max(1, min(days, MAX_DAYS))
    since = window_start(days, now)
    ids = [p.id for p in principals]
    if not ids:
        return []
    day = func.date(func.timezone("UTC", ToolCall.created_at))
    is_search = ToolCall.tool == "search"
    rows = session.execute(
        select(
            ToolCall.principal_id,
            day,
            ToolCall.tool,
            func.count(),
            func.count().filter(ToolCall.error.is_not(None)),
            func.count().filter(is_search, ToolCall.result["count"].as_integer() == 0),
        )
        .where(ToolCall.principal_id.in_(ids), ToolCall.created_at >= since)
        .group_by(ToolCall.principal_id, day, ToolCall.tool)
    ).all()
    last_used = {
        pid: last
        for pid, last in session.execute(
            select(ToolCall.principal_id, func.max(ToolCall.created_at))
            .where(ToolCall.principal_id.in_(ids))
            .group_by(ToolCall.principal_id)
        )
    }
    month_start, month_end = month_bounds(now)
    used = {
        pid: n
        for pid, n in session.execute(
            select(ToolCall.principal_id, func.count())
            .where(
                ToolCall.principal_id.in_(ids),
                ToolCall.created_at >= month_start,
                ToolCall.error.is_(None),
            )
            .group_by(ToolCall.principal_id)
        )
    }
    per_day: dict[int, dict[date, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0] * 4))
    per_tool: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for pid, d, tool, calls, errors, empty in rows:
        cell = per_day[pid][d]
        cell[0] += calls
        cell[1] += errors
        if tool == "search":
            cell[2] += calls
            cell[3] += empty
        per_tool[pid][tool][0] += calls
        per_tool[pid][tool][1] += errors
    out: list[ClientUsage] = []
    for p in principals:
        cells = per_day.get(p.id, {})
        daily: list[DayUsage] = []
        for i in range(days):
            d = (since + timedelta(days=i)).date()
            c = cells.get(d, [0, 0, 0, 0])
            daily.append(
                DayUsage(day=d, tool_calls=c[0], errors=c[1], searches=c[2], empty_searches=c[3])
            )
        tools = sorted(per_tool.get(p.id, {}).items(), key=lambda kv: (-kv[1][0], kv[0]))
        month_calls = int(used.get(p.id, 0))
        quota = p.monthly_query_quota
        out.append(
            ClientUsage(
                principal_id=p.id,
                name=p.name,
                scope=p.scope,
                suspended=p.suspended_at is not None,
                revoked=p.api_key_hash is None,
                days=days,
                since=since,
                tool_calls=sum(x.tool_calls for x in daily),
                errors=sum(x.errors for x in daily),
                searches=sum(x.searches for x in daily),
                empty_searches=sum(x.empty_searches for x in daily),
                last_used_at=last_used.get(p.id),
                month=MonthUsage(
                    period_start=month_start,
                    resets_at=month_end,
                    used=month_calls,
                    quota=quota,
                    remaining=None if quota is None else max(0, quota - month_calls),
                ),
                daily=daily,
                by_tool=[ToolUsage(tool=t, calls=v[0], errors=v[1]) for t, v in tools],
            )
        )
    return out
