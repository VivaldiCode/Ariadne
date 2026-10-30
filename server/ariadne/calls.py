"""Registo de cada chamada a uma ferramenta (tabela `tool_call`), com ou sem erro."""

import json
import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ariadne.db.models import ToolCall
from ariadne.textutil import clean_json, clean_text

logger = logging.getLogger(__name__)

ERROR_CHARS = 2000


@dataclass
class CallRecord:
    """O que a ferramenta preenche durante a chamada, para ir para `tool_call`."""

    result: dict[str, Any] | None = None  # só ids e contagens, nunca o texto devolvido
    query_id: int | None = None


@contextmanager
def tracked_call(
    session: Session,
    *,
    principal_id: int,
    session_id: str | None,
    tool: str,
    arguments: Mapping[str, Any],
) -> Iterator[CallRecord]:
    """Mede a chamada e grava `tool_call` no fim; uma exceção fica registada e volta a subir.

    Grava na mesma sessão da ferramenta (depois de um rollback, que descarta o que ficou a meio),
    por isso uma chamada nunca ocupa duas ligações à base de dados.
    """
    record = CallRecord()
    started = time.perf_counter()
    error: str | None = None
    try:
        yield record
    except Exception as exc:
        error = clean_text(f"{type(exc).__name__}: {exc}")[:ERROR_CHARS]
        raise
    finally:
        _write(
            session,
            ToolCall(
                principal_id=principal_id,
                session_id=clean_text(session_id) if session_id else None,
                tool=tool,
                arguments=clean_json(json.loads(json.dumps(arguments, default=str))),
                result=clean_json(record.result),
                query_id=record.query_id,
                latency_ms=round((time.perf_counter() - started) * 1000),
                error=error,
            ),
        )


def _write(session: Session, call: ToolCall) -> None:
    try:
        session.rollback()  # a ferramenta pode ter deixado uma transação falhada ou por fechar
        session.add(call)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not record call %s in tool_call", call.tool)
