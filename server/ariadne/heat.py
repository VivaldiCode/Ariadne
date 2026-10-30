"""Calor com decaimento (ARCHITECTURE.md, secção 6.1).

    heat(t) = heat(t_ant) · 2^(-(t - t_ant) / meia_vida) + peso_do_evento

Cada documento e cada chunk guarda `heat` e `heat_at` (o instante do último evento). A leitura
aplica o decaimento em tempo real (`current_heat`), por isso o worker `decay` só compacta: grava o
valor decaído e põe `heat_at = now()`, sem mudar o que a leitura devolve.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, Float, case, func, literal, select, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from ariadne.db.models import Chunk
from ariadne.db.models import Document as DocumentRow

HALF_LIFE_DAYS = 30.0
SECONDS_PER_DAY = 86_400.0

WEIGHT_SHOWN = 0.3  # o resultado foi mostrado numa pesquisa
WEIGHT_USED = 1.0  # o agente fez `read` do documento
WEIGHT_FEEDBACK = 2.0  # feedback positivo explícito

COMPACT_BATCH = 1000
COMPACT_MIN_AGE = timedelta(days=1)  # o que mudou há menos disto não precisa de compactar

# Abaixo disto o calor compacta para zero (já não distingue nada).
NEGLIGIBLE = 1e-6


def decayed(heat: float, last: datetime, now: datetime, half_life_days: float) -> float:
    """O mesmo cálculo em Python (para testes e para quem já tem os valores à mão)."""
    elapsed_days = max(0.0, (now - last).total_seconds() / SECONDS_PER_DAY)
    return float(heat * 2.0 ** (-elapsed_days / half_life_days))


def _decay_factor(heat_at: Any, half_life_days: float) -> ColumnElement[float]:
    elapsed = func.extract("epoch", func.now() - heat_at)
    return func.power(
        literal(2.0, Float), -func.greatest(elapsed, 0.0) / (half_life_days * 86_400.0)
    )


def current_heat(
    heat: InstrumentedAttribute[float],
    heat_at: InstrumentedAttribute[datetime],
    half_life_days: float = HALF_LIFE_DAYS,
) -> ColumnElement[float]:
    """Expressão SQL do calor neste instante: `heat · 2^(-(now - heat_at) / meia_vida)`."""
    return heat * _decay_factor(heat_at, half_life_days)


def document_heat(half_life_days: float = HALF_LIFE_DAYS) -> ColumnElement[float]:
    return current_heat(DocumentRow.heat, DocumentRow.heat_at, half_life_days)


def chunk_heat(half_life_days: float = HALF_LIFE_DAYS) -> ColumnElement[float]:
    return current_heat(Chunk.heat, Chunk.heat_at, half_life_days)


def add_document_heat(
    session: Session,
    document_ids: Sequence[str],
    weight: float,
    half_life_days: float = HALF_LIFE_DAYS,
) -> None:
    """Soma `weight` ao calor dos documentos, de forma atómica (um UPDATE: decai e soma)."""
    ids = sorted(set(document_ids))  # sempre pela mesma ordem: sem deadlocks entre pedidos
    if not ids:
        return
    session.execute(
        update(DocumentRow)
        .where(DocumentRow.id.in_(ids))
        .values(heat=document_heat(half_life_days) + weight, heat_at=func.now())
        .execution_options(synchronize_session=False)
    )


def add_chunk_heat(
    session: Session,
    chunk_ids: Sequence[int],
    weight: float,
    half_life_days: float = HALF_LIFE_DAYS,
) -> None:
    ids = sorted(set(chunk_ids))
    if not ids:
        return
    session.execute(
        update(Chunk)
        .where(Chunk.id.in_(ids))
        .values(heat=chunk_heat(half_life_days) + weight, heat_at=func.now())
        .execution_options(synchronize_session=False)
    )


def compact(
    session: Session,
    half_life_days: float = HALF_LIFE_DAYS,
    *,
    batch: int = COMPACT_BATCH,
    min_age: timedelta = COMPACT_MIN_AGE,
) -> tuple[int, int]:
    """Grava o calor decaído (`heat_at = now()`); o que ficou desprezível passa a zero.
    Devolve (documentos, chunks) tocados. A leitura em tempo real não muda.

    Faz-o em lotes de `batch` linhas (`FOR UPDATE SKIP LOCKED`, por ordem de id, commit por lote)
    e só nas que não mudam há mais de `min_age`: nunca segura locks globais nem pisa as linhas
    que um `search` está a aquecer neste momento."""
    touched: list[int] = []
    for model in (DocumentRow, Chunk):
        total = 0
        while True:
            value = current_heat(model.heat, model.heat_at, half_life_days)
            eligible = (
                select(model.id)
                .where(model.heat > 0, model.heat_at < func.now() - min_age)
                .order_by(model.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(model)
                    .where(model.id.in_(eligible.scalar_subquery()))
                    .values(heat=case((value < NEGLIGIBLE, 0.0), else_=value), heat_at=func.now())
                    .execution_options(synchronize_session=False)
                ),
            )
            session.commit()  # solta os locks do lote; o seguinte já não apanha estas linhas
            if result.rowcount == 0:
                break
            total += result.rowcount
        touched.append(total)
    return touched[0], touched[1]
