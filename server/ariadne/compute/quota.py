"""As horas de GPU gastas e as que restam na semana, por destino.

O Kaggle não expõe a quota por API, por isso o Ariadne conta o tempo das suas corridas (do
momento em que o destino as dá como a correr até ao fim). O que se gastou fora do Ariadne
(notebooks abertos à mão) corrige-se no painel: o dono diz «já gastei X h» e esse valor vale como
base até ao fim do período.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariadne.db.models import BatchRun

WEEK = timedelta(days=7)


@dataclass(frozen=True)
class Quota:
    weekly_hours: float
    used_hours: float
    remaining_hours: float
    period_start: datetime
    resets_at: datetime


def period_start(now: datetime, weekday: int) -> datetime:
    """O último `weekday` (0 = segunda … 6 = domingo) às 00:00 UTC que não passa de `now`."""
    now = now.astimezone(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def used_seconds(session: Session, backend: str, since: datetime, now: datetime) -> float:
    """Tempo de GPU das corridas deste destino desde `since`: as acabadas (`gpu_seconds`) e, para
    a que está a correr, o que já passou."""
    done = session.scalar(
        select(func.coalesce(func.sum(BatchRun.gpu_seconds), 0.0)).where(
            BatchRun.backend == backend,
            BatchRun.running_at >= since,
            BatchRun.gpu_seconds.is_not(None),
        )
    )
    running = session.scalars(
        select(BatchRun.running_at).where(
            BatchRun.backend == backend,
            BatchRun.status == "running",
            BatchRun.running_at.is_not(None),
        )
    ).all()
    live = sum(max(0.0, (now - started).total_seconds()) for started in running if started)
    return float(done or 0.0) + live


def compute_quota(
    session: Session, backend: str, config: dict[str, Any], now: datetime | None = None
) -> Quota | None:
    """A quota semanal do destino, ou `None` se o destino não tem uma (`weekly_hours` vazio)."""
    weekly = config.get("weekly_hours")
    if not weekly:
        return None
    now = now or datetime.now(UTC)
    start = period_start(now, int(config.get("reset_weekday", 5)))
    used = used_seconds(session, backend, start, now) / 3600
    baseline = config.get("used_hours_baseline")
    stamp = config.get("used_hours_baseline_at")
    if isinstance(baseline, int | float) and isinstance(stamp, str):
        set_at = datetime.fromisoformat(stamp)
        if set_at >= start:
            # O valor dado vale à hora em que foi dado; soma-se só o que o Ariadne gastou depois.
            used = float(baseline) + used_seconds(session, backend, set_at, now) / 3600
    weekly_hours = float(weekly)
    return Quota(
        weekly_hours=weekly_hours,
        used_hours=round(used, 3),
        remaining_hours=round(max(0.0, weekly_hours - used), 3),
        period_start=start,
        resets_at=start + WEEK,
    )
