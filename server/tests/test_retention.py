"""Retenção das perguntas dos clientes externos (chaves `mcp`): o texto vai-se aos 30 dias, as
contagens ficam."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, Scope, create_principal
from ariadne.config import Settings
from ariadne.db.models import QueryLog, ToolCall
from ariadne.retention import ERASED_ERROR, purge_client_queries

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def _log(session: Session, principal_id: int, days_ago: int, question: str) -> None:
    when = NOW - timedelta(days=days_ago)
    query = QueryLog(
        principal_id=principal_id,
        query=question,
        query_lang="pt",
        filters={"sources": ["zim:x"]},
        level_served=1,
        latency_ms=12,
        created_at=when,
    )
    session.add(query)
    session.flush()
    session.add(
        ToolCall(
            principal_id=principal_id,
            tool="search",
            arguments={"query": question},
            result={"query_id": query.id, "count": 2},
            query_id=query.id,
            latency_ms=12,
            created_at=when,
        )
    )
    session.add(
        ToolCall(
            principal_id=principal_id,
            tool="read",
            arguments={"document_id": question},
            error=f"Document not found: {question}",
            created_at=when,
        )
    )


def _seed(db: sessionmaker[Session]) -> tuple[int, int]:
    with db() as session, session.begin():
        client, _ = create_principal(session, PrincipalKind.agent, "cliente", scope=Scope.mcp)
        owner, _ = create_principal(session, PrincipalKind.agent, "agente-do-dono")
        for principal in (client, owner):
            _log(session, principal.id, 31, f"pergunta velha de {principal.name}")
            _log(session, principal.id, 29, f"pergunta recente de {principal.name}")
        return client.id, owner.id


def test_apaga_o_texto_das_perguntas_dos_clientes_aos_30_dias(db: sessionmaker[Session]) -> None:
    client, owner = _seed(db)

    with db() as session, session.begin():
        assert purge_client_queries(session, 30, now=NOW) == (1, 2)
    with db() as session, session.begin():
        assert purge_client_queries(session, 30, now=NOW) == (0, 0)  # idempotente

    with db() as session:
        queries = {(q.principal_id, q.query) for q in session.scalars(select(QueryLog))}
        calls = list(session.scalars(select(ToolCall).order_by(ToolCall.id)))
    assert queries == {
        (client, ""),
        (client, "pergunta recente de cliente"),
        (owner, "pergunta velha de agente-do-dono"),
        (owner, "pergunta recente de agente-do-dono"),
    }
    erased = [c for c in calls if c.principal_id == client and c.arguments == {}]
    assert [c.tool for c in erased] == ["search", "read"]
    search, read = erased
    assert search.result is not None and search.result["count"] == 2  # as métricas ficam
    assert search.latency_ms == 12 and search.query_id is not None
    assert read.error == ERASED_ERROR  # o erro ecoava o pedido
    assert sum(1 for c in calls if c.principal_id == client and c.arguments) == 2  # as recentes
    assert all(c.arguments for c in calls if c.principal_id == owner)  # as do dono, nunca


def test_o_worker_decay_faz_a_limpeza(db: sessionmaker[Session], database_url: str) -> None:
    from ariadne.services import Services
    from ariadne.sources.registry import SourceRegistry
    from ariadne.workers.decay import run_decay

    client, _ = _seed(db)
    with db() as session, session.begin():  # o worker usa a hora real: envelhece as linhas
        for row in session.scalars(select(QueryLog).where(QueryLog.principal_id == client)):
            row.created_at = datetime.now(UTC) - timedelta(days=60)
    settings = Settings(database_url=database_url, client_query_retention_days=30)
    services = Services(settings, db, SourceRegistry(Path("/nao-existe")), None)  # type: ignore[arg-type]

    run_decay(services)

    with db() as session:
        texts = set(session.scalars(select(QueryLog.query).where(QueryLog.principal_id == client)))
    assert texts == {""}
