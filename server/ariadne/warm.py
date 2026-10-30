"""Coleções a pré-aquecer: põe na fila de indexação uma lista de caminhos ou títulos de uma fonte
(o `ariadne index enqueue --paths` e `POST /api/collections` partilham isto)."""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ariadne.documents import make_document_id, read_article
from ariadne.semantic import SemanticStore
from ariadne.sources.base import NotAnArticle, NotFound, Source
from ariadne.workers.indexer import enqueue_index, recently_failed


def not_yet_wanted(
    session: Session, store: SemanticStore, model_id: str | None, document_ids: list[str]
) -> list[str]:
    """Dos documentos dados, os que ainda faz sentido pedir: sem os já indexados (modelo atual)
    nem os que falharam de vez há pouco."""
    done = store.indexed(session, document_ids, model_id) if model_id else set()
    gave_up = recently_failed(session, document_ids)
    return [d for d in document_ids if d not in done and d not in gave_up]


def seed_path(line: str, source_id: str) -> str:
    """Aceita o id completo do documento ('<fonte>/<caminho>') ou só o caminho/título."""
    prefix = f"{source_id}/"
    return line[len(prefix) :] if line.startswith(prefix) else line


@dataclass
class WarmReport:
    queued: int = 0
    known: int = 0  # já indexados, ou com falha recente
    ignored: list[str] = field(default_factory=list)  # entradas que não são artigos da fonte
    document_ids: list[str] = field(default_factory=list)  # os que ficaram na fila


def warm_entries(
    session: Session,
    source: Source,
    entries: list[str],
    *,
    priority: float,
    store: SemanticStore,
    model_id: str | None,
) -> WarmReport:
    """Resolve cada entrada (caminho ou título, redirects seguidos) e enfileira o que falta indexar.
    Não faz commit."""
    report = WarmReport()
    seen: set[str] = set()
    resolved: list[str] = []
    for line in entries:
        try:
            path = read_article(source, seed_path(line, source.id)).path
        except (NotFound, NotAnArticle):
            report.ignored.append(line)
            continue
        document_id = make_document_id(source.id, path)
        if document_id not in seen:
            seen.add(document_id)
            resolved.append(document_id)
    todo = not_yet_wanted(session, store, model_id, resolved)
    report.known = len(resolved) - len(todo)
    for document_id in todo:
        enqueue_index(session, document_id, priority)
        report.document_ids.append(document_id)
    report.queued = len(todo)
    return report
