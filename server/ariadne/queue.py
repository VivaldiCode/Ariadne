"""Fila de trabalho no Postgres (ARCHITECTURE.md, secção 10; decisão 14.8: nada de Redis).

Nenhuma função faz commit: quem chama decide a transação (o `search` enfileira na sua; o worker
faz commit depois de `claim` para o bloqueio ficar visível). Estados: pending -> running ->
done | failed; uma falha volta a pending com recuo exponencial até esgotar as tentativas.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, func, or_, select, text, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ariadne.db.models import Job
from ariadne.textutil import clean_json, clean_text

logger = logging.getLogger(__name__)

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"
# Exportado para uma corrida de GPU externa (`ariadne.compute`): nenhum worker o reclama; quando a
# corrida acaba, o que não foi consumido volta a `pending` (e passa ao curador local).
BATCHED = "batched"
BATCH_TRIED = "batch_tried"  # chave do payload: já foi a um lote externo (só o local o trata)
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_MAX = timedelta(hours=1)
ERROR_CHARS = 2000


class Defer(Exception):
    """O handler pede para o job voltar a correr mais tarde, sem contar como falha."""

    def __init__(self, run_after: datetime, reason: str = "") -> None:
        super().__init__(reason or f"deferred until {run_after.isoformat()}")
        self.run_after = run_after


class Permanent(Exception):
    """Falha sem remédio por repetir (ex.: a fonte já não existe): vai direto para `failed`."""


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int  # já conta com esta tentativa
    priority: float
    worker_id: str = ""  # quem o reclamou: só esse worker o pode concluir, falhar ou adiar


def backoff(attempts: int) -> timedelta:
    """30 s, 1 min, 2 min, … até 1 h."""
    return min(BACKOFF_BASE * (1 << max(0, attempts - 1)), BACKOFF_MAX)


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    priority: float = 0.0,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
) -> int:
    """Põe um job na fila. Com `dedupe_key`, se já há um pendente com a mesma chave não duplica:
    sobe-lhe a prioridade (a maior das duas). Devolve o id do job (novo ou existente)."""
    values: dict[str, Any] = {
        "kind": kind,
        "payload": clean_json(payload),
        "priority": priority,
        "dedupe_key": clean_text(dedupe_key) if dedupe_key else None,
    }
    if run_after is not None:
        values["run_after"] = run_after
    stmt = insert(Job).values(**values)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_update(
            index_elements=["dedupe_key"],
            index_where=text("status = 'pending'"),
            set_={"priority": func.greatest(Job.priority, stmt.excluded.priority)},
        )
    return int(session.execute(stmt.returning(Job.id)).scalar_one())


def active_by_key(session: Session, kind: str, dedupe_key: str) -> int | None:
    """O job pendente **ou a correr** com esta chave (a deduplicação do `enqueue` só vê os
    pendentes: sem isto, o mesmo trabalho corria duas vezes, uma já em curso e outra à espera)."""
    return session.scalar(
        select(Job.id)
        .where(
            Job.kind == kind,
            Job.dedupe_key == dedupe_key,
            Job.status.in_((PENDING, RUNNING, BATCHED)),
        )
        .order_by(Job.id)
        .limit(1)
    )


def claim(
    session: Session,
    kinds: list[str],
    worker_id: str,
    *,
    tried_only_kinds: list[str] | None = None,
) -> ClaimedJob | None:
    """Reclama o job pendente mais prioritário e já devido, ou `None`. `FOR UPDATE SKIP LOCKED`
    deixa vários workers reclamar em simultâneo sem se pisarem. Os `tried_only_kinds` só servem
    os jobs que já passaram por um lote externo (`batch_tried` no payload): os outros esperam
    pelo lote (a regra «primeiro consome lá, depois consome aqui»)."""
    kind_filter: ColumnElement[bool] = Job.kind.in_(kinds)
    if tried_only_kinds:
        kind_filter = or_(
            kind_filter,
            and_(Job.kind.in_(tried_only_kinds), cast(Any, Job.payload).has_key(BATCH_TRIED)),
        )
    next_id = (
        select(Job.id)
        .where(Job.status == PENDING, kind_filter, Job.run_after <= func.now())
        .order_by(Job.priority.desc(), Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    row = session.execute(
        update(Job)
        .where(Job.id == next_id)
        .values(
            status=RUNNING,
            attempts=Job.attempts + 1,
            locked_at=func.now(),
            locked_by=worker_id,
        )
        .returning(Job.id, Job.kind, Job.payload, Job.attempts, Job.priority)
    ).first()
    if row is None:
        return None
    return ClaimedJob(row.id, row.kind, row.payload, row.attempts, row.priority, worker_id)


def touch(session: Session, job_id: int, worker_id: str) -> bool:
    """Renova o bloqueio de um job longo, para não ser dado como preso. `False` se o job já não
    é deste worker (foi recuperado como preso e entregue a outro)."""
    return _update(session, job_id, worker_id, locked_at=func.now())


def patch_payload(
    session: Session, job_id: int, worker_id: str | None, patch: dict[str, Any]
) -> bool:
    """Junta `patch` ao `payload` do job (o progresso de um job longo, um pedido de cancelamento).
    Com `worker_id` só se o job ainda é deste worker, e renova-lhe o bloqueio (como `touch`)."""
    values: dict[str, Any] = {"payload": Job.payload.op("||")(sql_cast(clean_json(patch), JSONB))}
    if worker_id is not None:
        values["locked_at"] = func.now()
    return _update(session, job_id, worker_id, **values)


def complete(session: Session, job_id: int, worker_id: str) -> bool:
    return _update(
        session,
        job_id,
        worker_id,
        status=DONE,
        finished_at=func.now(),
        locked_at=None,
        locked_by=None,
    )


def fail(
    session: Session,
    job_id: int,
    error: str,
    worker_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    permanent: bool = False,
) -> str | None:
    """Regista a falha: volta a pending com recuo exponencial, ou `failed` se acabaram as
    tentativas (ou a falha é `permanent`). Devolve o novo estado, ou `None` se o job já não é
    deste worker."""
    attempts = session.scalar(
        select(Job.attempts).where(Job.id == job_id, *_owned(worker_id)).with_for_update()
    )
    if attempts is None:
        return None
    message = clean_text(error)[:ERROR_CHARS]
    if permanent or attempts >= max_attempts:
        _update(
            session,
            job_id,
            worker_id,
            status=FAILED,
            last_error=message,
            finished_at=func.now(),
            locked_at=None,
            locked_by=None,
        )
        return FAILED
    return _requeue(session, job_id, datetime.now(UTC) + backoff(attempts), message, worker_id)


def defer(
    session: Session, job_id: int, run_after: datetime, worker_id: str, reason: str = ""
) -> str | None:
    """Volta a pôr o job em pending para `run_after`, sem gastar uma tentativa."""
    return _requeue(
        session,
        job_id,
        run_after,
        clean_text(reason)[:ERROR_CHARS] or None,
        worker_id,
        refund_attempt=True,
    )


def recover_stale(session: Session, older_than: timedelta, max_attempts: int) -> int:
    """Jobs 'running' bloqueados há mais de `older_than` (worker morto) voltam à fila, ou falham
    se já gastaram as tentativas. Devolve quantos foram recuperados."""
    rows = session.execute(
        select(Job.id, Job.attempts)
        .where(Job.status == RUNNING, Job.locked_at < func.now() - older_than)
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
    ).all()
    for job_id, attempts in rows:
        message = "worker disappeared (lock expired)"
        if attempts >= max_attempts:
            _update(
                session,
                job_id,
                None,
                status=FAILED,
                last_error=message,
                finished_at=func.now(),
                locked_at=None,
                locked_by=None,
            )
        else:
            _requeue(session, job_id, datetime.now(UTC), message, None)
    return len(rows)


def counts(session: Session) -> dict[str, int]:
    """Quantos jobs há em cada estado (para estatísticas)."""
    rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    return {status: n for status, n in rows}


def counts_by_kind(session: Session) -> dict[str, dict[str, int]]:
    """Quantos jobs há por tipo e estado: `{'index_document': {'pending': 3, 'done': 10}}`."""
    rows = session.execute(
        select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status)
    )
    result: dict[str, dict[str, int]] = {}
    for kind, status, n in rows:
        result.setdefault(kind, {})[status] = n
    return result


def failed(session: Session, limit: int) -> list[Job]:
    """Os jobs que falharam de vez, dos mais recentes para os mais antigos."""
    return list(
        session.scalars(
            select(Job)
            .where(Job.status == FAILED)
            .order_by(Job.finished_at.desc().nulls_last(), Job.id.desc())
            .limit(limit)
        )
    )


def retry(session: Session, job_id: int) -> str:
    """Põe um job `failed` outra vez na fila, com as tentativas a zero. Devolve `retried`,
    `not_failed` (o job existe mas não está falhado), `missing` ou `duplicate` (já há um job
    pendente com a mesma chave: esse cobre o trabalho)."""
    job = session.get(Job, job_id, with_for_update=True)
    if job is None:
        return "missing"
    if job.status != FAILED:
        return "not_failed"
    try:
        with session.begin_nested():
            session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    status=PENDING,
                    attempts=0,
                    run_after=func.now(),
                    locked_at=None,
                    locked_by=None,
                    finished_at=None,
                    last_error=None,
                )
            )
    except IntegrityError:
        return "duplicate"
    return "retried"


def upcoming(session: Session, limit: int) -> list[Job]:
    """Os pendentes pela ordem em que o `claim` os serve: primeiro os já devidos, por prioridade;
    depois os adiados (`run_after` no futuro, ex.: para amanhã), também por prioridade."""
    return list(
        session.scalars(
            select(Job)
            .where(Job.status == PENDING)
            .order_by(Job.run_after > func.now(), Job.priority.desc(), Job.run_after, Job.id)
            .limit(limit)
        )
    )


def mark_batched(session: Session, job_ids: list[int]) -> int:
    """Tira da fila os jobs pendentes que seguem para uma corrida externa. Devolve quantos."""
    if not job_ids:
        return 0
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Job)
            .where(Job.id.in_(job_ids), Job.status == PENDING)
            .values(status=BATCHED, locked_at=func.now(), locked_by="batch")
        ),
    )
    return result.rowcount


def release_batched(
    session: Session, job_ids: list[int], *, tried: bool = True, done: bool = False
) -> int:
    """Devolve ao fim da corrida os jobs `batched`: `done=True` conclui-os (o resultado foi
    aplicado); senão voltam a `pending`, marcados `batch_tried` (o curador local trata deles).
    Se entretanto entrou outro pendente com a mesma chave, o job dá-se por feito."""
    released = 0
    for job_id in job_ids:
        job = session.get(Job, job_id, with_for_update=True)
        if job is None or job.status != BATCHED:
            continue
        released += 1
        if done:
            job.status, job.finished_at = DONE, func.now()
        else:
            job.status, job.run_after = PENDING, func.now()
            if tried:
                job.payload = {**job.payload, BATCH_TRIED: True}
        job.locked_at, job.locked_by = None, None
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            session.refresh(job)
            job.status, job.finished_at = DONE, func.now()
            job.last_error = "replaced by another pending job with the same key"
            session.flush()
    return released


def _owned(worker_id: str | None) -> list[ColumnElement[bool]]:
    """Só se mexe num job que está `running` e é deste worker (`None`: sem verificar, para a
    recuperação de presos)."""
    if worker_id is None:
        return []
    return [Job.status == RUNNING, Job.locked_by == worker_id]


def _update(session: Session, job_id: int, worker_id: str | None, **values: Any) -> bool:
    result = cast(
        "CursorResult[Any]",
        session.execute(update(Job).where(Job.id == job_id, *_owned(worker_id)).values(**values)),
    )
    return result.rowcount == 1


def _requeue(
    session: Session,
    job_id: int,
    run_after: datetime,
    message: str | None,
    worker_id: str | None,
    *,
    refund_attempt: bool = False,
) -> str | None:
    """Volta a pending. Se entretanto entrou outro pendente com a mesma chave, este já não faz
    falta: dá-se por feito (o outro cobre o trabalho). `None` se o job já não é deste worker."""
    values: dict[str, Any] = {
        "status": PENDING,
        "run_after": run_after,
        "last_error": message,
        "locked_at": None,
        "locked_by": None,
    }
    if refund_attempt:
        values["attempts"] = func.greatest(Job.attempts - 1, 0)
    try:
        with session.begin_nested():
            if not _update(session, job_id, worker_id, **values):
                return None
    except IntegrityError:
        _update(
            session,
            job_id,
            worker_id,
            status=DONE,
            last_error="replaced by another pending job with the same key",
            finished_at=func.now(),
            locked_at=None,
            locked_by=None,
        )
        return DONE
    return PENDING
