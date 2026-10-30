"""Worker `neighbors`: expande a vizinhança dos documentos quentes (ARCHITECTURE.md, secção 6).

Periodicamente escolhe os documentos cujo calor atual passou `H_viz` e que ainda não foram
expandidos (ou cuja última expansão tem mais de uma meia-vida e continuam quentes), e enfileira a
indexação dos vizinhos: adjacência, links internos do ZIM, ligações cruzadas, sala e semântico.
Um salto só: os vizinhos não ganham calor por terem sido enfileirados, por isso só o uso os faz
expandir também. A prioridade de cada `index_document` é o calor da origem × o peso do tipo; o
orçamento diário do indexer continua a ser o travão global.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne import heat, router
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Link
from ariadne.documents import (
    DocumentInfo,
    LibraryError,
    make_document_id,
    read_article,
    split_document_id,
    upsert_documents,
)
from ariadne.services import Services
from ariadne.sources.base import NotAnArticle, NotFound, Source, SourceError
from ariadne.workers.indexer import enqueue_index, recently_failed

logger = logging.getLogger(__name__)

CREATED_BY = "neighbors"
ADJACENCY, ZIM_INTERNAL, CROSS, ROOM, SEMANTIC = (
    "adjacency",
    "zim_internal",
    "cross",
    "room",
    "semantic",
)


@dataclass
class Expansion:
    """O que uma expansão fez (para o registo e para os testes)."""

    document_id: str
    heat: float
    enqueued: dict[str, float] = field(default_factory=dict)  # document_id -> prioridade
    links: list[str] = field(default_factory=list)  # destinos das arestas zim_internal novas


@dataclass
class _Candidate:
    document_id: str
    priority: float
    order: int  # ordem de descoberta: desempata a prioridade
    kinds: set[str] = field(default_factory=set)


class NeighborsError(Exception):
    """A expansão não se pôde fazer (ex.: a fonte já não está no registo): o documento volta a
    ficar por expandir e tenta-se de novo numa ronda seguinte."""


@dataclass(frozen=True)
class _Claimed:
    document_id: str
    heat: float
    room: int | None
    previous: datetime | None  # o `expanded_at` de antes, para repor se a expansão falhar


def run_neighbors(services: Services, stop: threading.Event | None = None) -> list[Expansion]:
    """Uma ronda: expande até `NEIGHBORS_BATCH` documentos quentes, do mais quente para o menos.
    Olha para `stop` entre expansões."""
    settings = services.settings
    stop = stop or threading.Event()
    model_id = router.current_model_id(services)
    if model_id is None:  # sem embedder o índice não cresce: espera-se, nada se marca como feito
        logger.info("Neighbors: embedder unavailable, the round is postponed")
        return []
    with services.sessions() as session:
        services.registry.sync_if_changed(
            session
        )  # uma fonte nova ou removida desde a última ronda
        session.commit()
    done: list[Expansion] = []
    skipped: set[str] = set()
    while len(done) < settings.neighbors_batch and not stop.is_set():
        with services.sessions() as session:
            claimed = _claim_hot(services, session, skipped)
            session.commit()  # a marca fica visível e o lock da linha solta-se já: o resto é lento
            if claimed is None:
                break
            try:
                expansion = _expand(services, session, model_id, claimed)
                session.commit()
            except Exception:
                session.rollback()
                _release(session, claimed)
                skipped.add(claimed.document_id)
                logger.exception("Neighbors: expansion of %s failed", claimed.document_id)
                continue
        done.append(expansion)
        logger.info(
            "Neighbors of %s (heat %.1f): %d to index, %d new links",
            claimed.document_id,
            claimed.heat,
            len(expansion.enqueued),
            len(expansion.links),
        )
    return done


def _claim_hot(services: Services, session: Session, skipped: set[str]) -> _Claimed | None:
    """Reclama o documento mais quente por expandir: um só `UPDATE … expanded_at = now()` sobre a
    linha escolhida com `FOR UPDATE SKIP LOCKED`, por isso dois workers nunca expandem o mesmo e o
    lock dura só o statement (o `search` e o indexer não ficam à espera durante as leituras do
    ZIM). O pré-filtro `heat >= H_viz` descarta quase tudo sem fazer contas."""
    settings = services.settings
    half_life = settings.heat_half_life_days
    current = heat.document_heat(half_life)
    stale = func.now() - timedelta(days=half_life)
    conditions: list[ColumnElement[bool]] = [
        DocumentRow.heat >= settings.neighbors_heat_threshold,
        current >= settings.neighbors_heat_threshold,
        or_(DocumentRow.expanded_at.is_(None), DocumentRow.expanded_at < stale),
    ]
    if skipped:
        conditions.append(DocumentRow.id.not_in(skipped))
    picked = (
        select(
            DocumentRow.id.label("id"),
            current.label("heat"),
            DocumentRow.room.label("room"),
            DocumentRow.expanded_at.label("previous"),
        )
        .where(*conditions)
        .order_by(current.desc(), DocumentRow.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=DocumentRow)
        .cte("picked")
    )
    row = session.execute(
        update(DocumentRow)
        .where(DocumentRow.id == picked.c.id)
        .values(expanded_at=func.now())
        .returning(DocumentRow.id, picked.c.heat, picked.c.room, picked.c.previous)
        .execution_options(synchronize_session=False)
    ).first()
    if row is None:
        return None
    return _Claimed(row[0], float(row[1]), row[2], row[3])


def _release(session: Session, claimed: _Claimed) -> None:
    """A expansão falhou: o documento volta ao estado de antes e fica por expandir."""
    session.execute(
        update(DocumentRow)
        .where(DocumentRow.id == claimed.document_id)
        .values(expanded_at=claimed.previous)
    )
    session.commit()


def _expand(services: Services, session: Session, model_id: str, claimed: _Claimed) -> Expansion:
    settings = services.settings
    document_id, document_heat, room = claimed.document_id, claimed.heat, claimed.room
    expansion = Expansion(document_id, document_heat)
    try:
        source_id, path = split_document_id(document_id)
    except LibraryError as exc:
        raise NeighborsError(str(exc)) from exc
    source = services.registry.get(source_id)
    if source is None:
        raise NeighborsError(f"source {source_id!r} is not in the registry")

    # Adjacência: o próprio documento quente tem de ficar todo indexado.
    incomplete = document_id not in services.store.indexed(session, [document_id], model_id)
    if incomplete and document_id not in recently_failed(session, [document_id]):
        priority = document_heat * settings.neighbors_weight_adjacency
        enqueue_index(session, document_id, priority)
        expansion.enqueued[document_id] = priority

    candidates: dict[str, _Candidate] = {}

    def offer(neighbor: str, kind: str, weight: float) -> None:
        if neighbor == document_id:
            return
        priority = document_heat * weight
        found = candidates.setdefault(neighbor, _Candidate(neighbor, priority, len(candidates)))
        found.priority = max(found.priority, priority)
        found.kinds.add(kind)

    titles_in_zim = _internal_links(
        source, path
    )  # caminho canónico -> título, pela ordem do artigo
    for linked in titles_in_zim:
        offer(make_document_id(source.id, linked), ZIM_INTERNAL, settings.neighbors_weight_links)
    crossed = session.execute(  # as arestas valem nos dois sentidos
        select(Link.src_document, Link.dst_document)
        .where(
            or_(Link.src_document == document_id, Link.dst_document == document_id),
            Link.status == "accepted",
            Link.kind != ZIM_INTERNAL,
        )
        .order_by(Link.score.desc().nulls_last(), Link.id)
    )
    for src, dst in crossed:
        offer(dst if src == document_id else src, CROSS, settings.neighbors_weight_cross)
    if room is not None:
        hottest = session.scalars(
            select(DocumentRow.id)
            .where(DocumentRow.room == room, DocumentRow.id != document_id)
            .order_by(heat.document_heat(settings.heat_half_life_days).desc(), DocumentRow.id)
            .limit(settings.neighbors_room_limit)
        )
        for roommate in hottest:
            offer(roommate, ROOM, settings.neighbors_weight_room)
    for similar in services.store.similar_documents(
        session, document_id, model_id, settings.neighbors_semantic_k
    ):
        offer(similar, SEMANTIC, settings.neighbors_weight_semantic)
    if not candidates:
        return expansion

    ids = list(candidates)
    skip = recently_failed(session, ids)  # falhou de vez há pouco: não insistir
    indexed = services.store.indexed(session, ids, model_id)
    have_edge = set(
        session.scalars(
            select(Link.dst_document).where(
                Link.src_document == document_id,
                Link.kind == ZIM_INTERNAL,
                Link.dst_document.in_(ids),
            )
        )
    )
    # Só conta para o limite o que traz trabalho novo (indexar, ou uma aresta por registar).
    ranked = sorted(candidates.values(), key=lambda c: (-c.priority, c.order))
    chosen: list[_Candidate] = []
    titles: dict[str, str] = {}
    for candidate in ranked:
        if len(chosen) >= settings.neighbors_max_per_doc:
            break
        needs_edge = ZIM_INTERNAL in candidate.kinds and candidate.document_id not in have_edge
        needs_index = candidate.document_id not in indexed and candidate.document_id not in skip
        if not (needs_edge or needs_index):
            continue
        if needs_edge:  # o título do destino já veio nos links do artigo: não se relê
            titles[candidate.document_id] = titles_in_zim[
                split_document_id(candidate.document_id)[1]
            ]
        chosen.append(candidate)

    new_documents = [
        DocumentInfo(source, split_document_id(d)[1], title) for d, title in titles.items()
    ]
    upsert_documents(session, new_documents)
    if titles:
        stmt = insert(Link).values(
            [
                {
                    "src_document": document_id,
                    "dst_document": target,
                    "kind": ZIM_INTERNAL,
                    "score": 1.0,
                    "status": "accepted",
                    "created_by": CREATED_BY,
                }
                for target in sorted(titles)
            ]
        )
        session.execute(
            stmt.on_conflict_do_nothing(index_elements=["src_document", "dst_document", "kind"])
        )
        expansion.links = sorted(titles)
    for candidate in chosen:
        if candidate.document_id in indexed or candidate.document_id in skip:
            continue
        enqueue_index(session, candidate.document_id, candidate.priority)
        expansion.enqueued[candidate.document_id] = candidate.priority
    return expansion


def _internal_links(source: Source, path: str) -> dict[str, str]:
    """Os artigos da mesma fonte ligados a partir de `path` (caminho canónico -> título do
    destino), sem repetições e pela ordem em que aparecem. Uma só leitura do artigo quente."""
    try:
        article = read_article(source, path)
    except (NotFound, NotAnArticle):
        return {}
    except SourceError:
        logger.warning("Neighbors: could not read %s/%s", source.id, path, exc_info=True)
        return {}
    found: dict[str, str] = {}
    for link in article.links:
        found.setdefault(link.path, link.title)
    return found
