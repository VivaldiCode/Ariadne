"""Alas e salas — consulta (ARCHITECTURE.md, secções 4.5 e 10).

A ala é a fonte (`source.wing`); a sala é um grupo temático de artigos dentro da ala, calculado por
`ariadne.rooms_build` a partir dos vetores de artigo. Aqui estão os modelos que a pesquisa, o
`sources` e a API devolvem, e as consultas que os preenchem.
"""

from collections.abc import Mapping, Sequence

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Room

DEFAULT_LIST_LIMIT = 200
SOURCES_TOP = 10  # salas por ala em `sources` (as maiores)


class RoomRef(BaseModel):
    """A sala de um documento, nos resultados da pesquisa e na leitura."""

    id: int
    label: str


class RoomInfo(BaseModel):
    id: int
    wing: str
    label: str
    size: int  # documentos agora


class WingRooms(BaseModel):
    """As salas de uma ala, no catálogo de fontes."""

    count: int
    top: list[RoomInfo]  # as maiores


def list_rooms(
    session: Session,
    *,
    wing: str | None = None,
    model_id: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[RoomInfo]:
    """As salas, das maiores para as mais pequenas. `model_id` limita às do modelo atual (as de
    outro modelo estão à espera de ser recalculadas)."""
    stmt = select(Room.id, Room.wing, Room.label, Room.size).where(Room.ready)
    if wing is not None:
        stmt = stmt.where(Room.wing == wing)
    if model_id is not None:
        stmt = stmt.where(Room.model_id == model_id)
    rows = session.execute(stmt.order_by(Room.size.desc(), Room.id).limit(max(1, limit))).all()
    return [RoomInfo(id=r.id, wing=r.wing, label=r.label, size=r.size) for r in rows]


def rooms_by_wing(
    session: Session,
    wings: Sequence[str],
    *,
    model_id: str | None = None,
    top: int = SOURCES_TOP,
) -> dict[str, WingRooms]:
    """Para cada ala dada, quantas salas tem e as `top` maiores."""
    if not wings:
        return {}
    rank = (
        func.row_number()
        .over(partition_by=Room.wing, order_by=(Room.size.desc(), Room.id))
        .label("rank")
    )
    total = func.count().over(partition_by=Room.wing).label("total")
    stmt = select(Room.id, Room.wing, Room.label, Room.size, rank, total).where(
        Room.wing.in_(list(wings)), Room.ready
    )
    if model_id is not None:
        stmt = stmt.where(Room.model_id == model_id)
    ranked = stmt.subquery()
    rows = session.execute(
        select(ranked).where(ranked.c.rank <= top).order_by(ranked.c.wing, ranked.c.rank)
    ).all()
    found: dict[str, WingRooms] = {}
    for row in rows:
        entry = found.setdefault(row.wing, WingRooms(count=row.total, top=[]))
        entry.top.append(RoomInfo(id=row.id, wing=row.wing, label=row.label, size=row.size))
    return found


def rooms_of(session: Session, document_ids: Sequence[str]) -> dict[str, RoomRef]:
    """document_id -> sala, para os documentos que a têm."""
    if not document_ids:
        return {}
    rows = session.execute(
        select(DocumentRow.id, Room.id, Room.label)
        .join(Room, (Room.id == DocumentRow.room) & Room.ready)
        .where(DocumentRow.id.in_(list(document_ids)))
    )
    return {document_id: RoomRef(id=room_id, label=label) for document_id, room_id, label in rows}


def wings_of_rooms(session: Session, room_ids: Sequence[int]) -> dict[int, str]:
    """room_id -> ala, para as salas que existem (e já estão prontas)."""
    if not room_ids:
        return {}
    rows = session.execute(
        select(Room.id, Room.wing).where(Room.id.in_(list(room_ids)), Room.ready)
    )
    return {room_id: wing for room_id, wing in rows}


def likely_rooms(
    session: Session,
    query_vector: Sequence[float],
    model_id: str,
    wings: Sequence[str],
    *,
    top: int,
) -> Mapping[int, float]:
    """As `top` salas (das alas dadas, do modelo atual) cujo centróide está mais perto da
    pergunta: room_id -> semelhança cosseno. Vazio se ainda não há salas."""
    if not wings or top < 1:
        return {}
    distance = Room.centroid.cosine_distance(list(query_vector))
    rows = session.execute(
        select(Room.id, distance)
        .where(Room.wing.in_(list(wings)), Room.model_id == model_id, Room.ready)
        .order_by(distance, Room.id)
        .limit(top)
    ).all()
    return {room_id: 1.0 - float(dist) for room_id, dist in rows}


def room_ids_of_documents(session: Session, document_ids: Sequence[str]) -> dict[str, int]:
    """document_id -> id da sala, só para os que têm sala."""
    if not document_ids:
        return {}
    rows = session.execute(
        select(DocumentRow.id, DocumentRow.room).where(
            DocumentRow.id.in_(list(document_ids)), DocumentRow.room.is_not(None)
        )
    )
    return {document_id: room for document_id, room in rows if room is not None}
