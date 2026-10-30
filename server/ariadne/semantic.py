"""Armazenamento do nível 3 atrás de uma interface mínima (ARCHITECTURE.md, secção 14, ponto 9).

`SemanticStore` é o que o router e o indexador precisam do índice vetorial; `PgVectorStore` guarda
tudo em `chunk` + `chunk_vector` (pgvector, HNSW cosseno). A Fase 9 põe o MemPalace atrás da mesma
interface sem tocar no router.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne.db.models import Chunk, ChunkVector, DocumentVector, Link
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Source as SourceRow
from ariadne.relink import RELINK
from ariadne.textutil import clean_text

# Candidatos pedidos ao HNSW antes de filtrar por fonte: max(MIN_CANDIDATES, k · OVERSAMPLE).
MIN_CANDIDATES = 100
OVERSAMPLE = 5
# Vizinhos de artigo de outras fontes: começa-se por MIN_CANDIDATES e aprofunda-se (×4) enquanto
# os candidatos mais fundos ainda passam o limiar e ainda não há `limit` de outras fontes.
CROSS_SOURCE_MAX_DEPTH = 1600
HNSW_MAX_EF_SEARCH = 1000  # o máximo que o pgvector aceita


@dataclass(frozen=True)
class SemanticHit:
    chunk_id: int
    document_id: str
    title: str
    source_id: str
    path: str
    ord: int
    text: str
    similarity: float  # cosseno: 1 = igual


@dataclass(frozen=True)
class PendingChunk:
    chunk_id: int
    text: str


class SemanticStore(Protocol):
    def put_chunks(self, session: Session, document_id: str, texts: Sequence[str]) -> None:
        """Grava os chunks do documento (idempotente por posição); os que mudaram de texto
        perdem os vetores, que deixaram de valer."""
        ...

    def missing_vectors(
        self, session: Session, document_id: str, model_id: str
    ) -> list[PendingChunk]:
        """Chunks do documento que ainda não têm vetor deste modelo, por ordem."""
        ...

    def put_vectors(
        self, session: Session, model_id: str, vectors: Mapping[int, Sequence[float]]
    ) -> None:
        """Grava vetores por `chunk_id` (os que já existem ficam como estão)."""
        ...

    def count_vectors_since(self, session: Session, model_id: str, since: datetime) -> int:
        """Quantos vetores deste modelo foram criados desde `since` (orçamento diário)."""
        ...

    def search(
        self,
        session: Session,
        vector: Sequence[float],
        *,
        model_id: str,
        k: int,
        source_ids: Sequence[str],
        room_ids: Sequence[int] | None = None,
    ) -> list[SemanticHit]:
        """Os `k` chunks mais próximos entre as fontes dadas, do mais para o menos parecido; com
        `room_ids`, só de documentos dessas salas."""
        ...

    def mark_indexed(self, session: Session, document_id: str, model_id: str, version: str) -> None:
        """O documento tem todos os chunks vetorizados com este modelo (nesta versão)."""
        ...

    def indexed(self, session: Session, document_ids: Sequence[str], model_id: str) -> set[str]:
        """Dos documentos dados, os que estão completos para este modelo e na versão atual."""
        ...

    def similar_documents(
        self, session: Session, document_id: str, model_id: str, k: int
    ) -> list[str]:
        """Os `k` documentos mais próximos (vetor de artigo) do dado, do mais para o menos
        parecido, de fontes ativas e sem uma ligação rejeitada com o dado; vazio se o documento
        não tem vetor de artigo deste modelo."""
        ...

    def nearest_documents(
        self, session: Session, document_id: str, model_id: str, k: int
    ) -> list[tuple[str, float]]:
        """Como `similar_documents`, com a semelhança cosseno de cada um (1 = igual)."""
        ...

    def similar_across_sources(
        self,
        session: Session,
        document_id: str,
        model_id: str,
        min_similarity: float,
        limit: int,
    ) -> list[tuple[str, float]]:
        """Até `limit` documentos de OUTRAS fontes (ativas) com semelhança ≥ `min_similarity` ao
        dado (vetor de artigo), do mais para o menos parecido; vazio sem vetor deste modelo."""
        ...

    def cosine_similarities(
        self, session: Session, model_id: str, pairs: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], float]:
        """A semelhança cosseno dos vetores de artigo de cada par, para os pares em que os dois
        têm vetor deste modelo."""
        ...

    def put_article_vectors(
        self, session: Session, model_id: str, vectors: Mapping[str, Sequence[float]]
    ) -> int:
        """Grava vetores de artigo por `document_id` (os documentos têm de existir). Idempotente:
        um vetor igual não se toca; um que mudou é substituído. Os documentos novos ou alterados
        voltam à fila do `linker`. Devolve quantos foram gravados ou alterados."""
        ...

    def has_article_vectors(
        self, session: Session, document_ids: Sequence[str], model_id: str
    ) -> set[str]:
        """Dos documentos dados, os que já têm vetor de artigo deste modelo."""
        ...


def _rejected_with(session: Session, document_id: str, others: Sequence[str]) -> set[str]:
    """Dos documentos dados, os que têm uma aresta rejeitada (qualquer tipo, qualquer sentido)
    com `document_id`: a curadoria disse que não, nem como vizinho semântico voltam."""
    if not others:
        return set()
    found: set[str] = set()
    for src, dst in session.execute(
        select(Link.src_document, Link.dst_document).where(
            Link.status == "rejected",
            or_(
                (Link.src_document == document_id) & Link.dst_document.in_(list(others)),
                (Link.dst_document == document_id) & Link.src_document.in_(list(others)),
            ),
        )
    ):
        found.add(dst if src == document_id else src)
    return found


class PgVectorStore:
    def put_chunks(self, session: Session, document_id: str, texts: Sequence[str]) -> None:
        clean = [clean_text(t) for t in texts]
        if clean:
            stmt = insert(Chunk).values(
                [{"document_id": document_id, "ord": i, "text": t} for i, t in enumerate(clean)]
            )
            changed = session.scalars(
                stmt.on_conflict_do_update(
                    index_elements=["document_id", "ord"],
                    set_={"text": stmt.excluded.text},
                    where=Chunk.text.is_distinct_from(stmt.excluded.text),
                ).returning(Chunk.id)
            ).all()
            if changed:
                session.execute(delete(ChunkVector).where(ChunkVector.chunk_id.in_(changed)))
        # O documento encolheu: os chunks a mais deixam de existir para a pesquisa. A linha fica
        # (um query_result antigo pode apontar para ela) com ord = -id, fora do intervalo válido.
        orphans = select(Chunk.id).where(Chunk.document_id == document_id, Chunk.ord >= len(clean))
        session.execute(delete(ChunkVector).where(ChunkVector.chunk_id.in_(orphans)))
        session.execute(
            update(Chunk)
            .where(Chunk.document_id == document_id, Chunk.ord >= len(clean))
            .values(ord=-Chunk.id)
        )

    def missing_vectors(
        self, session: Session, document_id: str, model_id: str
    ) -> list[PendingChunk]:
        rows = session.execute(
            select(Chunk.id, Chunk.text)
            .outerjoin(
                ChunkVector,
                (ChunkVector.chunk_id == Chunk.id) & (ChunkVector.model_id == model_id),
            )
            .where(Chunk.document_id == document_id, Chunk.ord >= 0, ChunkVector.chunk_id.is_(None))
            .order_by(Chunk.ord)
        ).all()
        return [PendingChunk(chunk_id, chunk_text) for chunk_id, chunk_text in rows]

    def put_vectors(
        self, session: Session, model_id: str, vectors: Mapping[int, Sequence[float]]
    ) -> None:
        if not vectors:
            return
        stmt = insert(ChunkVector).values(
            [
                {"chunk_id": chunk_id, "model_id": model_id, "embedding": list(vector)}
                for chunk_id, vector in sorted(vectors.items())
            ]
        )
        session.execute(stmt.on_conflict_do_nothing(index_elements=["chunk_id", "model_id"]))

    def count_vectors_since(self, session: Session, model_id: str, since: datetime) -> int:
        return (
            session.scalar(
                select(func.count())
                .select_from(ChunkVector)
                .where(ChunkVector.model_id == model_id, ChunkVector.created_at >= since)
            )
            or 0
        )

    def search(
        self,
        session: Session,
        vector: Sequence[float],
        *,
        model_id: str,
        k: int,
        source_ids: Sequence[str],
        room_ids: Sequence[int] | None = None,
    ) -> list[SemanticHit]:
        if not source_ids or (room_ids is not None and not room_ids):
            return []
        distance = ChunkVector.embedding.cosine_distance(list(vector))
        columns = (
            ChunkVector.chunk_id,
            distance.label("distance"),
            Chunk.document_id,
            Chunk.ord,
            Chunk.text,
            DocumentRow.title,
            DocumentRow.source_id,
            DocumentRow.path,
        )
        if room_ids is not None:
            # Dentro de salas a conta é exata: só há vetores de chunk onde o uso os criou, e um
            # filtro apertado à saída do HNSW deixaria a sala quase sempre vazia.
            rows = session.execute(
                select(*columns)
                .join(Chunk, Chunk.id == ChunkVector.chunk_id)
                .join(DocumentRow, DocumentRow.id == Chunk.document_id)
                .where(
                    ChunkVector.model_id == model_id,
                    DocumentRow.source_id.in_(list(source_ids)),
                    DocumentRow.room.in_(list(room_ids)),
                )
                .order_by(distance, ChunkVector.chunk_id)
                .limit(k)
            ).all()
        else:
            rows = self._nearest_chunks(session, distance, model_id, k, source_ids)
        return [
            SemanticHit(
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                title=row.title,
                source_id=row.source_id,
                path=row.path,
                ord=row.ord,
                text=row.text,
                similarity=1.0 - float(row.distance),
            )
            for row in rows
        ]

    @staticmethod
    def _nearest_chunks(
        session: Session, distance: Any, model_id: str, k: int, source_ids: Sequence[str]
    ) -> Sequence[Any]:
        # O HNSW percorre-se sozinho (só `chunk_vector`, filtrado pelo modelo): com joins no meio
        # o planner deixava de usar o índice. pgvector 0.8: o filtro por modelo é aplicado depois
        # de percorrer o grafo, e sem o varrimento iterativo o índice podia devolver menos de
        # `candidates` linhas (recall estragado). `relaxed_order` não garante a ordem exata, por
        # isso reordena-se por fora. O filtro por fonte fica à saída, daí o sobreamostrar.
        session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        nearest = (
            select(ChunkVector.chunk_id.label("chunk_id"), distance.label("distance"))
            .where(ChunkVector.model_id == model_id)
            .order_by(distance)
            .limit(max(MIN_CANDIDATES, k * OVERSAMPLE))
            .cte("nearest")
            .prefix_with("MATERIALIZED")
        )
        return session.execute(
            select(
                nearest.c.chunk_id,
                nearest.c.distance,
                Chunk.document_id,
                Chunk.ord,
                Chunk.text,
                DocumentRow.title,
                DocumentRow.source_id,
                DocumentRow.path,
            )
            .join(Chunk, Chunk.id == nearest.c.chunk_id)
            .join(DocumentRow, DocumentRow.id == Chunk.document_id)
            .where(DocumentRow.source_id.in_(list(source_ids)))
            .order_by(nearest.c.distance, nearest.c.chunk_id)
            .limit(k)
        ).all()

    def mark_indexed(self, session: Session, document_id: str, model_id: str, version: str) -> None:
        session.execute(
            text(
                "UPDATE document SET meta = jsonb_set(meta, '{indexed}',"
                " COALESCE(meta->'indexed', '{}'::jsonb)"
                " || jsonb_build_object(CAST(:model AS text), CAST(:version AS text)))"
                " WHERE id = :id"
            ),
            {"model": model_id, "version": version, "id": document_id},
        )

    def indexed(self, session: Session, document_ids: Sequence[str], model_id: str) -> set[str]:
        if not document_ids:
            return set()
        rows = session.scalars(
            select(DocumentRow.id).where(
                DocumentRow.id.in_(list(document_ids)),
                DocumentRow.meta["indexed"][model_id].astext == DocumentRow.version,
            )
        )
        return set(rows)

    def similar_documents(
        self, session: Session, document_id: str, model_id: str, k: int
    ) -> list[str]:
        return [found for found, _ in self.nearest_documents(session, document_id, model_id, k)]

    def nearest_documents(
        self, session: Session, document_id: str, model_id: str, k: int
    ) -> list[tuple[str, float]]:
        return self._scan(session, document_id, model_id, k, floor=-1.0, same_source=True)

    def similar_across_sources(
        self,
        session: Session,
        document_id: str,
        model_id: str,
        min_similarity: float,
        limit: int,
    ) -> list[tuple[str, float]]:
        return self._scan(
            session, document_id, model_id, limit, floor=min_similarity, same_source=False
        )

    def _scan(
        self,
        session: Session,
        document_id: str,
        model_id: str,
        limit: int,
        *,
        floor: float,
        same_source: bool,
    ) -> list[tuple[str, float]]:
        """kNN nos vetores de artigo: até `limit` documentos com semelhança ≥ `floor`, de fontes
        ativas, sem uma aresta `rejected` com o dado e (se `not same_source`) só de outras fontes.

        Sem `iterative_scan` e um `ef_search` à altura o HNSW devolveria no máximo 40 linhas (e,
        filtrado por modelo, menos). Os filtros fazem-se por fora e a maioria dos vizinhos pode
        ser da própria fonte, por isso aprofunda-se (×4) enquanto o fundo da lista ainda passa
        `floor` e ainda não há `limit` elegíveis."""
        own = self._own_vector(session, document_id, model_id)
        source_id = session.scalar(
            select(DocumentRow.source_id)
            .join(SourceRow, SourceRow.id == DocumentRow.source_id)
            .where(DocumentRow.id == document_id, SourceRow.status == "active")
        )
        if own is None or source_id is None or limit <= 0:
            return []
        session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        distance = DocumentVector.embedding.cosine_distance(own)
        depth = max(MIN_CANDIDATES, limit * OVERSAMPLE)
        while True:
            session.execute(
                text("SELECT set_config('hnsw.ef_search', :n, true)"),
                {"n": str(min(depth, HNSW_MAX_EF_SEARCH))},
            )
            rows = session.execute(
                select(DocumentVector.document_id, distance)
                .where(
                    DocumentVector.model_id == model_id, DocumentVector.document_id != document_id
                )
                .order_by(distance, DocumentVector.document_id)
                .limit(depth)
            ).all()
            sims = [(found, 1.0 - float(dist)) for found, dist in rows]
            ids = [found for found, _ in sims]
            sources = dict(
                session.execute(
                    select(DocumentRow.id, DocumentRow.source_id)
                    .join(SourceRow, SourceRow.id == DocumentRow.source_id)
                    .where(DocumentRow.id.in_(ids), SourceRow.status == "active")
                ).all()
            )
            rejected = _rejected_with(session, document_id, ids)
            eligible = [
                (found, sim)
                for found, sim in sims
                if sim >= floor
                and found in sources
                and found not in rejected
                and (same_source or sources[found] != source_id)
            ]
            exhausted = len(rows) < depth or sims[-1][1] < floor
            if exhausted or len(eligible) >= limit or depth >= CROSS_SOURCE_MAX_DEPTH:
                break
            depth *= 4
        eligible.sort(key=lambda pair: (-pair[1], pair[0]))
        return eligible[:limit]

    def cosine_similarities(
        self, session: Session, model_id: str, pairs: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], float]:
        if not pairs:
            return {}
        rows = session.execute(
            text(
                "SELECT x.a, x.b, 1 - (va.embedding <=> vb.embedding)"
                " FROM unnest(CAST(:a AS text[]), CAST(:b AS text[])) AS x(a, b)"
                " JOIN document_vector va ON va.document_id = x.a AND va.model_id = :model"
                " JOIN document_vector vb ON vb.document_id = x.b AND vb.model_id = :model"
            ),
            {"a": [a for a, _ in pairs], "b": [b for _, b in pairs], "model": model_id},
        )
        return {(a, b): float(sim) for a, b, sim in rows}

    def put_article_vectors(
        self, session: Session, model_id: str, vectors: Mapping[str, Sequence[float]]
    ) -> int:
        if not vectors:
            return 0
        stmt = insert(DocumentVector).values(
            [
                {"document_id": document_id, "model_id": model_id, "embedding": list(vector)}
                for document_id, vector in sorted(vectors.items())
            ]
        )
        changed = session.scalars(
            stmt.on_conflict_do_update(
                index_elements=["document_id", "model_id"],
                set_={"embedding": stmt.excluded.embedding},
                where=DocumentVector.embedding != stmt.excluded.embedding,
            ).returning(DocumentVector.document_id)
        ).all()
        if changed:  # um vetor novo pode trazer ligações novas: o linker volta a ver o documento
            session.execute(update(DocumentRow).where(DocumentRow.id.in_(changed)).values(**RELINK))
        return len(changed)

    def has_article_vectors(
        self, session: Session, document_ids: Sequence[str], model_id: str
    ) -> set[str]:
        if not document_ids:
            return set()
        return set(
            session.scalars(
                select(DocumentVector.document_id).where(
                    DocumentVector.model_id == model_id,
                    DocumentVector.document_id.in_(list(document_ids)),
                )
            )
        )

    @staticmethod
    def _own_vector(session: Session, document_id: str, model_id: str) -> list[float] | None:
        own = session.scalar(
            select(DocumentVector.embedding).where(
                DocumentVector.document_id == document_id, DocumentVector.model_id == model_id
            )
        )
        return None if own is None else list(own)
