"""Retenção e apagamento dos dados dos clientes externos (ARCHITECTURE.md, «Chaves de clientes
externos»).

As chaves `mcp` são de clientes da versão alojada: o texto do que perguntam (`query_log.query` e
`filters`, `tool_call.arguments` e as mensagens de erro, que podem ecoar o pedido) apaga-se ao fim
de `CLIENT_QUERY_RETENTION_DAYS` (30). Ficam as linhas, com o principal, a ferramenta, a latência,
o nível servido e os ids e contagens dos resultados: as métricas de uso por chave. As perguntas das
chaves `full` (as do dono) não se tocam. A pedido do cliente (RGPD, «apagar conta» no portal),
`erase_client` apaga-o de vez, com todo o histórico (`POST /api/principals/{id}/erase`).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, cast, delete, null, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from ariadne.auth import Scope
from ariadne.db.models import Collection, Principal, QueryLog, QueryResult, ToolCall

ERASED_ERROR = "(apagado: retenção)"
ERASED_NAME = "(apagado a pedido)"


def purge_client_queries(
    session: Session, retention_days: int, now: datetime | None = None
) -> tuple[int, int]:
    """Apaga o texto das perguntas das chaves `mcp` mais velhas do que `retention_days`; devolve
    quantas linhas de `query_log` e de `tool_call` mudaram. Idempotente."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    clients = select(Principal.id).where(Principal.scope == Scope.mcp).scalar_subquery()
    queries = session.execute(
        update(QueryLog)
        .where(
            QueryLog.principal_id.in_(clients),
            QueryLog.created_at < cutoff,
            (QueryLog.query != "") | QueryLog.filters.is_not(None),
        )
        .values(query="", filters=null())
        .execution_options(synchronize_session=False)
    )
    calls = session.execute(
        update(ToolCall)
        .where(
            ToolCall.principal_id.in_(clients),
            ToolCall.created_at < cutoff,
            (ToolCall.arguments != {})
            | (ToolCall.error.is_not(None) & (ToolCall.error != ERASED_ERROR)),
        )
        .values(arguments={}, error=case((ToolCall.error.is_(None), None), else_=ERASED_ERROR))
        .execution_options(synchronize_session=False)
    )
    return queries.rowcount, calls.rowcount  # type: ignore[attr-defined]


class EraseRefused(Exception):
    """O principal não é de um cliente externo (âmbito `full`): o dono nunca se apaga assim."""


def erase_client(session: Session, principal_id: int) -> dict[str, int] | None:
    """Apaga um cliente externo (RGPD, «apagar conta»): o principal (nome e hash da chave), as
    chamadas (`tool_call`), as pesquisas (`query_log`) com os resultados e o feedback
    (`query_result`), e o nome e o `external_id` dele no registo de quem o criou
    (`principal_create`). O calor que as pesquisas dele deram aos documentos não é pessoal e fica.
    Devolve as contagens, ou None se o principal já não existe (idempotente). Não faz commit."""
    principal = session.get(Principal, principal_id, with_for_update=True, populate_existing=True)
    if principal is None:
        return None
    if principal.scope != Scope.mcp:
        raise EraseRefused(principal_id)
    queries = select(QueryLog.id).where(QueryLog.principal_id == principal_id).scalar_subquery()
    results = session.execute(delete(QueryResult).where(QueryResult.query_id.in_(queries)))
    session.execute(  # outra chamada a apontar para estas pesquisas fica sem a ligação
        update(ToolCall)
        .where(ToolCall.query_id.in_(queries), ToolCall.principal_id != principal_id)
        .values(query_id=None)
    )
    calls = session.execute(delete(ToolCall).where(ToolCall.principal_id == principal_id))
    logged = session.execute(delete(QueryLog).where(QueryLog.principal_id == principal_id))
    session.execute(
        update(Collection).where(Collection.principal_id == principal_id).values(principal_id=None)
    )
    created = ToolCall.result["principal_id"].as_integer() == principal_id
    session.execute(
        update(ToolCall)
        .where(ToolCall.tool == "principal_create", created)
        .values(
            arguments=ToolCall.arguments.op("||")(
                cast({"name": ERASED_NAME, "external_id": ERASED_NAME}, JSONB)
            )
        )
    )
    session.delete(principal)
    session.flush()
    return {
        "tool_calls": calls.rowcount,  # type: ignore[attr-defined]
        "queries": logged.rowcount,  # type: ignore[attr-defined]
        "query_results": results.rowcount,  # type: ignore[attr-defined]
    }
