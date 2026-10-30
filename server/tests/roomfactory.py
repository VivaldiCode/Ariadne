"""Alas e salas de brincar: documentos com vetores de artigo em grupos bem separados."""

import math

from sqlalchemy.orm import Session

from ariadne.db.models import DocumentVector
from ariadne.documents import DocumentInfo, upsert_documents
from ariadne.embedder import EMBEDDING_DIM
from ariadne.services import Services

MODEL = "fake/embedder@0000000"


def cluster_vector(axis: int, noise: float = 0.0, side: int = 100) -> list[float]:
    """Vetor de norma 1 no eixo `axis`, afastado dele por `noise` (na direção do eixo `side`)."""
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = math.sqrt(1 - noise**2)
    vector[side] = noise
    return vector


def seed_documents(
    services: Services,
    session: Session,
    source_id: str,
    groups: dict[int, list[str]],
    *,
    model_id: str = MODEL,
) -> list[str]:
    """Um documento por título, com o vetor do grupo (o eixo) e um ruído que cresce com a posição:
    o 1.º título de cada grupo é o mais central. Devolve os document_id pela ordem dada."""
    source = services.registry.get(source_id)
    assert source is not None
    ids: list[str] = []
    for axis, titles in groups.items():
        upsert_documents(session, [DocumentInfo(source, t, t) for t in titles])
        for position, title in enumerate(titles):
            document_id = f"{source_id}/{title}"
            session.add(
                DocumentVector(
                    document_id=document_id,
                    model_id=model_id,
                    embedding=cluster_vector(axis, 0.01 * position, side=100 + position),
                )
            )
            ids.append(document_id)
    session.commit()
    return ids
