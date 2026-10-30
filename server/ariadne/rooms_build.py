"""Cálculo das salas (ARCHITECTURE.md, secção 4.5): k-means esférico sobre os vetores de artigo.

Para cada ala (`source.wing`) agrupam-se os vetores de artigo (`document_vector`) do modelo atual
em ~√(n/2) salas. Cada sala tem um centróide, um rótulo (os 3 títulos mais perto do centróide) e um
id que sobrevive aos recálculos: a sala nova herda o id da antiga com o centróide mais parecido.
Entre recálculos, os documentos novos vão para a sala do centróide mais próximo. O recálculo faz-se
quando a ala cresceu `ROOMS_GROWTH` desde o último.

Uma ala calcula-se de cada vez (um lock consultivo do Postgres por ala), por isso vários workers não
duplicam salas.
"""

import logging
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import case, delete, func, insert, select, text, update
from sqlalchemy.orm import Session

from ariadne import router
from ariadne.clustering import (
    Matrix,
    match_centroids,
    nearest,
    normalize,
    room_count,
    spherical_kmeans,
)
from ariadne.config import Settings
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import DocumentVector, Room
from ariadne.db.models import Source as SourceRow
from ariadne.services import Services

logger = logging.getLogger(__name__)

LABEL_TITLES = 3
LABEL_MAX_CHARS = 160
LOAD_BATCH = 20_000  # vetores lidos da base de cada vez


@dataclass
class WingReport:
    wing: str
    documents: int = 0  # artigos com vetor do modelo atual
    rooms: int = 0
    created: int = 0
    kept: int = 0  # salas que herdaram o id de uma antiga
    removed: int = 0
    assigned: int = 0  # documentos atribuídos (sem recálculo)
    rebuilt: bool = False
    skipped: str | None = None  # porque não se fez nada
    busy: bool = False  # outro processo está a calcular esta ala


@dataclass
class RoomsRun:
    wings: list[WingReport] = field(default_factory=list)


# --- ler ---------------------------------------------------------------------------------------


def wing_counts(session: Session, model_id: str) -> dict[str, int]:
    """Alas ativas -> artigos com vetor do modelo."""
    rows = session.execute(
        select(SourceRow.wing, func.count())
        .select_from(DocumentVector)
        .join(DocumentRow, DocumentRow.id == DocumentVector.document_id)
        .join(SourceRow, SourceRow.id == DocumentRow.source_id)
        .where(DocumentVector.model_id == model_id, SourceRow.status == "active")
        .group_by(SourceRow.wing)
        .order_by(SourceRow.wing)
    )
    return {wing: count for wing, count in rows}


def vectorized_documents(session: Session) -> dict[str, int]:
    """Alas ativas -> artigos com vetor de qualquer modelo (a base de comparação da troca de
    modelo)."""
    rows = session.execute(
        select(SourceRow.wing, func.count(func.distinct(DocumentVector.document_id)))
        .select_from(DocumentVector)
        .join(DocumentRow, DocumentRow.id == DocumentVector.document_id)
        .join(SourceRow, SourceRow.id == DocumentRow.source_id)
        .where(SourceRow.status == "active")
        .group_by(SourceRow.wing)
    )
    return {wing: count for wing, count in rows}


def _other_model_rooms(session: Session, wing: str, model_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Room)
            .where(Room.wing == wing, Room.model_id != model_id, Room.ready)
        )
        or 0
    )


def _rooms_state(session: Session, wing: str, model_id: str) -> tuple[int, int]:
    """(salas do modelo, soma do tamanho que tinham quando foram calculadas)."""
    count, base = session.execute(
        select(func.count(), func.coalesce(func.sum(Room.base_size), 0)).where(
            Room.wing == wing, Room.model_id == model_id, Room.ready
        )
    ).one()
    return int(count), int(base)


def needs_build(settings: Settings, documents: int, rooms: int, base: int) -> bool:
    """Falta calcular as salas da ala, ou ela cresceu `rooms_growth` desde que se calcularam."""
    if documents < settings.rooms_min_documents:
        return False
    if rooms == 0 or base <= 0:
        return True
    return documents >= base * (1 + settings.rooms_growth)


def _vector_rows(
    session: Session, wing: str, model_id: str, *, after: str, limit: int, stride: int = 1
) -> tuple[list[str], Matrix]:
    stmt = (
        select(DocumentVector.document_id, DocumentVector.embedding)
        .join(DocumentRow, DocumentRow.id == DocumentVector.document_id)
        .join(SourceRow, SourceRow.id == DocumentRow.source_id)
        .where(
            SourceRow.wing == wing,
            SourceRow.status == "active",
            DocumentVector.model_id == model_id,
            DocumentVector.document_id > after,
        )
        .order_by(DocumentVector.document_id)
        .limit(limit)
    )
    if stride > 1:  # amostra determinística: o resto da divisão do hash do id (sinal à parte)
        stmt = stmt.where(
            func.mod(func.hashtextextended(DocumentVector.document_id, 0), stride) == 0
        )
    rows = session.execute(stmt).all()
    ids = [row[0] for row in rows]
    matrix = (
        normalize(np.stack([np.asarray(row[1], dtype=np.float32) for row in rows]))
        if rows
        else np.empty((0, 0), dtype=np.float32)
    )
    return ids, matrix


def _iter_vectors(
    services: Services, wing: str, model_id: str, *, stride: int = 1, stop: threading.Event | None
) -> Iterator[tuple[list[str], Matrix]]:
    """Os vetores da ala por lotes (paginação por id); cada lote com a sua sessão curta."""
    after = ""
    while stop is None or not stop.is_set():
        with services.sessions() as session:
            ids, matrix = _vector_rows(
                session, wing, model_id, after=after, limit=LOAD_BATCH, stride=stride
            )
        if not ids:
            return
        yield ids, matrix
        after = ids[-1]


# --- calcular ----------------------------------------------------------------------------------


def wing_seed(settings: Settings, wing: str) -> int:
    return settings.rooms_seed * 1_000_003 + zlib.crc32(wing.encode())


@contextmanager
def wing_lock(services: Services, wing: str) -> Iterator[bool]:
    """Lock consultivo (por ala) numa sessão à parte que fica aberta até ao fim; `False` se outro
    processo já está a calcular esta ala."""
    key = f"ariadne:rooms:{wing}"
    with services.sessions() as holder:
        got = bool(
            holder.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": key})
        )
        try:
            yield got
        finally:
            if got:
                holder.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key}
                )
            holder.rollback()


def build_wing(
    services: Services, wing: str, model_id: str, *, stop: threading.Event | None = None
) -> WingReport:
    """(Re)calcula as salas da ala. Escreve por passos, cada um na sua transação: primeiro as salas
    (centróides, com o id das antigas quando se parecem), depois a sala de cada documento, e por fim
    rótulos e tamanhos e a remoção das salas que ficaram sem herdeira."""
    settings = services.settings
    report = WingReport(wing, rebuilt=True)
    with services.sessions() as session:
        report.documents = wing_counts(session, model_id).get(wing, 0)
        old = session.execute(
            select(Room.id, Room.model_id, Room.centroid, Room.label, Room.ready)
            .where(Room.wing == wing)
            .order_by(Room.id)
        ).all()
    if report.documents < settings.rooms_min_documents:
        report.skipped = (
            f"no rooms: only {report.documents} articles with a vector "
            f"(minimum {settings.rooms_min_documents})"
        )
        return report

    k = room_count(report.documents, lowest=settings.rooms_min, highest=settings.rooms_max)
    stride = max(1, -(-report.documents // settings.rooms_train_sample))
    sample = [m for _, m in _iter_vectors(services, wing, model_id, stride=stride, stop=stop)]
    if not sample or (stop is not None and stop.is_set()):
        report.skipped = "interrupted"
        return report
    training = np.concatenate(sample)
    del sample
    centroids, _ = spherical_kmeans(training, k, seed=wing_seed(settings, wing))
    center = training.mean(axis=0, dtype=np.float32)
    del training
    # As antigas (prontas, do mesmo modelo) cedem o id às novas que se parecem com elas.
    same_model = [row for row in old if row.model_id == model_id and row.ready]
    old_matrix = (
        normalize(np.stack([np.asarray(r.centroid, dtype=np.float32) for r in same_model]))
        if same_model
        else np.empty((0, 0), dtype=np.float32)
    )
    pairs = match_centroids(
        old_matrix, centroids, minimum=settings.rooms_match_similarity, center=center
    )
    # As salas novas nascem `ready = false` (invisíveis) e as herdadas só mudam no fim: quem
    # pesquisa a meio do cálculo vê as salas de antes, nunca rótulos vazios.
    room_ids: list[int] = []
    with services.sessions() as session:
        for index, centroid in enumerate(centroids):
            if index in pairs:
                room_ids.append(same_model[pairs[index]].id)
                continue
            room_ids.append(
                session.execute(
                    insert(Room)
                    .values(
                        wing=wing,
                        label="",
                        model_id=model_id,
                        centroid=centroid.tolist(),
                        ready=False,
                    )
                    .returning(Room.id)
                ).scalar_one()
            )
        session.commit()

    sizes = np.zeros(len(centroids), dtype=np.int64)
    best: dict[int, list[tuple[float, str]]] = {i: [] for i in range(len(centroids))}
    for ids, matrix in _iter_vectors(services, wing, model_id, stop=stop):
        labels, sims = nearest(matrix, centroids)
        _write_rooms(services, ids, [room_ids[i] for i in labels.tolist()])
        sizes += np.bincount(labels, minlength=len(centroids))
        _keep_most_central(best, ids, labels, sims)
    if stop is not None and stop.is_set():
        report.skipped = "interrupted midway: the new rooms stay hidden until the next round"
        return report

    # Com a ala toda atribuída, um centróide treinado numa amostra pode ter ficado sem ninguém:
    # nunca se grava uma sala vazia.
    filled = [i for i in range(len(centroids)) if sizes[i] > 0]
    titles = _titles(services, [doc for entries in best.values() for _, doc in entries])
    old_by_id = {row.id: row for row in old}
    keep = {room_ids[i] for i in filled}
    with services.sessions() as session:
        for index in filled:
            room_id = room_ids[index]
            members = sorted(best[index], key=lambda pair: (-pair[0], pair[1]))
            label = make_label([titles.get(doc, doc) for _, doc in members])
            values: dict[str, Any] = {
                "label": label,
                "size": int(sizes[index]),
                "base_size": int(sizes[index]),
                "ready": True,
            }
            previous = old_by_id.get(room_id)
            if previous is None:  # nova
                report.created += 1
                values["updated_at"] = func.now()
            else:  # herdada: o centróide novo entra agora
                report.kept += 1
                values["centroid"] = centroids[index].tolist()
                # `updated_at` só anda se o rótulo ou o centróide mudaram (o espelho do MemPalace
                # refaz a ala por causa dele).
                if np.array_equal(
                    np.asarray(previous.centroid, dtype=np.float32), centroids[index]
                ):
                    values["updated_at"] = case(
                        (Room.label.is_distinct_from(label), func.now()), else_=Room.updated_at
                    )
                else:
                    values["updated_at"] = func.now()
            session.execute(update(Room).where(Room.id == room_id).values(**values))
        gone = [r.id for r in old if r.id not in keep]
        # Também as que ficaram vazias, as de outro modelo e as meio-feitas de um cálculo anterior.
        empty_new = [room_ids[i] for i in range(len(centroids)) if i not in set(filled)]
        gone += [i for i in empty_new if i not in old_by_id]
        if gone:  # o `ON DELETE SET NULL` solta os documentos que ficaram só com a sala antiga
            session.execute(delete(Room).where(Room.id.in_(gone)))
        session.commit()
    report.rooms = len(filled)
    report.removed = len([i for i in gone if i in old_by_id])
    logger.info(
        "Rooms of %s: %d documents in %d rooms (%d new, %d kept, %d removed)",
        wing,
        report.documents,
        report.rooms,
        report.created,
        report.kept,
        report.removed,
    )
    return report


def _write_rooms(services: Services, ids: list[str], rooms: list[int]) -> None:
    with services.sessions() as session:
        session.execute(
            text(
                "UPDATE document SET room = x.room"
                " FROM unnest(CAST(:ids AS text[]), CAST(:rooms AS bigint[])) AS x(id, room)"
                " WHERE document.id = x.id AND document.room IS DISTINCT FROM x.room"
            ),
            {"ids": ids, "rooms": rooms},
        )
        session.commit()


def _keep_most_central(
    best: dict[int, list[tuple[float, str]]],
    ids: list[str],
    labels: NDArray[np.intp],
    sims: NDArray[np.float32],
) -> None:
    """Acumula, por sala, os `LABEL_TITLES` documentos mais perto do centróide."""
    order = np.lexsort((-sims, labels))  # por sala, do mais para o menos central
    sorted_labels = labels[order]
    starts = np.flatnonzero(np.r_[True, sorted_labels[1:] != sorted_labels[:-1]])
    ends = np.r_[starts[1:], len(order)]
    for start, end in zip(starts.tolist(), ends.tolist(), strict=True):
        room = int(sorted_labels[start])
        for position in order[start : min(end, start + LABEL_TITLES)].tolist():
            best[room].append((float(sims[position]), ids[position]))
        best[room] = sorted(best[room], key=lambda pair: (-pair[0], pair[1]))[:LABEL_TITLES]


def _titles(services: Services, document_ids: list[str]) -> dict[str, str]:
    if not document_ids:
        return {}
    with services.sessions() as session:
        return dict(
            session.execute(
                select(DocumentRow.id, DocumentRow.title).where(DocumentRow.id.in_(document_ids))
            ).all()
        )


def make_label(titles: list[str]) -> str:
    """Os títulos mais centrais, sem repetições, juntos com « · » e cortados num tamanho legível."""
    seen: list[str] = []
    for title in titles:
        title = " ".join(title.split())
        if title and title not in seen:
            seen.append(title)
    label = " · ".join(seen[:LABEL_TITLES]) or "(untitled)"
    if len(label) > LABEL_MAX_CHARS:
        label = label[: LABEL_MAX_CHARS - 1].rstrip() + "…"
    return label


# --- atribuição incremental --------------------------------------------------------------------


def assign_pending(
    services: Services, wing: str, model_id: str, *, stop: threading.Event | None = None
) -> int:
    """Põe cada documento da ala que tem vetor mas não tem sala na sala do centróide mais próximo.
    Devolve quantos atribuiu; sem salas na ala não faz nada."""
    batch = services.settings.rooms_assign_batch
    total = 0
    with services.sessions() as session:
        has_rooms = session.scalar(
            select(func.count())
            .select_from(Room)
            .where(Room.wing == wing, Room.model_id == model_id)
        )
    if not has_rooms:
        return 0
    while stop is None or not stop.is_set():
        with services.sessions() as session:
            result = session.execute(
                text(
                    "WITH todo AS ("
                    " SELECT d.id, dv.embedding FROM document d"
                    " JOIN source s ON s.id = d.source_id"
                    " JOIN document_vector dv ON dv.document_id = d.id AND dv.model_id = :model"
                    " WHERE s.wing = :wing AND s.status = 'active' AND d.room IS NULL"
                    " ORDER BY d.id LIMIT :batch FOR UPDATE OF d SKIP LOCKED)"
                    " UPDATE document SET room = ("
                    "   SELECT r.id FROM room r"
                    "   WHERE r.wing = :wing AND r.model_id = :model AND r.ready"
                    "   ORDER BY r.centroid <=> todo.embedding, r.id LIMIT 1)"
                    " FROM todo WHERE document.id = todo.id"
                ),
                {"wing": wing, "model": model_id, "batch": batch},
            )
            assigned = int(result.rowcount or 0)  # type: ignore[attr-defined]
            session.commit()
        total += assigned
        if assigned < batch:
            break
    if total:
        refresh_sizes(services, wing)
    return total


def refresh_sizes(services: Services, wing: str) -> None:
    """Recontagem dos documentos de cada sala da ala."""
    with services.sessions() as session:
        session.execute(
            text(
                # Sem `updated_at`: só o rótulo e o centróide contam como mudança da sala.
                "UPDATE room SET size = c.n FROM ("
                " SELECT r.id, (SELECT count(*) FROM document d WHERE d.room = r.id) AS n"
                " FROM room r WHERE r.wing = :wing) c"
                " WHERE room.id = c.id AND room.size IS DISTINCT FROM c.n"
            ),
            {"wing": wing},
        )
        session.commit()


# --- o worker ----------------------------------------------------------------------------------


def run_rooms(
    services: Services,
    stop: threading.Event | None = None,
    *,
    wing: str | None = None,
    force: bool = False,
) -> RoomsRun:
    """Uma ronda: em cada ala, atribui os documentos novos e recalcula as salas se a ala cresceu
    (ou se ainda não tem). `force` recalcula sempre; `wing` limita a uma ala."""
    stop = stop or threading.Event()
    run = RoomsRun()
    model_id = router.current_model_id(services)
    if model_id is None:
        logger.info("Rooms: embedder unavailable, the round is postponed")
        return run
    with services.sessions() as session:
        counts = wing_counts(session, model_id)
        total = vectorized_documents(session)
    for name, documents in counts.items():
        if stop.is_set():
            break
        if wing is not None and name != wing:
            continue
        with wing_lock(services, name) as got:
            if not got:
                run.wings.append(
                    WingReport(
                        name, documents=documents, busy=True, skipped="a calcular noutro processo"
                    )
                )
                continue
            report = WingReport(name, documents=documents)
            with services.sessions() as session:
                rooms, base = _rooms_state(session, name, model_id)
                replacing = rooms == 0 and _other_model_rooms(session, name, model_id) > 0
            coverage = documents / total[name] if total.get(name) else 1.0
            if replacing and not force and coverage < services.settings.rooms_model_switch_coverage:
                # Modelo novo: as salas antigas servem até o novo cobrir quase toda a ala.
                report.skipped = (
                    f"the new model covers {coverage:.0%} of the wing's articles (minimum "
                    f"{services.settings.rooms_model_switch_coverage:.0%}): the old rooms stay"
                )
            elif force or needs_build(services.settings, documents, rooms, base):
                report = build_wing(services, name, model_id, stop=stop)
            else:
                report.assigned = assign_pending(services, name, model_id, stop=stop)
                report.rooms = rooms
            run.wings.append(report)
    return run
