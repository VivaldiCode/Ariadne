"""Adaptador MemPalace atrás do `SemanticStore` (ARCHITECTURE.md, secções 4.5 e 17).

O que o pacote `mempalace` (3.11) permite, verificado a ler-lhe a API:

* o backend pgvector guarda as gavetas (*drawers*) numa tabela dele (`mempalace_<hash>_<coleção>`,
  com `id`, `document`, `metadata jsonb` e `embedding`), não em tabelas nossas: usá-lo como índice
  único obrigaria a duplicar chunks e vetores;
* o pacote depende do `chromadb` (binários nativos, não validados sem AVX) mesmo com o backend
  pgvector, e o backend ainda está em *preview*.

Por isso as tabelas do Ariadne (`chunk`, `chunk_vector`, `document_vector`, `room`) continuam a
ser a fonte de verdade e o `MemPalaceStore` **é** o `PgVectorStore` (pesquisa e escrita iguais) com
uma operação a mais: `sync`, o espelho unidirecional Ariadne → MemPalace. Cada chunk vira uma
gaveta com `wing` (a ala), `room` (o rótulo da sala) e `room_id`, para as ferramentas do MemPalace
poderem navegar por alas e salas. Os vetores espelhados são os do Ariadne: as consultas por texto
do próprio MemPalace usam o modelo dele e não os encontram bem; o espelho serve a navegação e a
exportação.

O pacote importa-se à última hora (extra `ariadne[mempalace]`); sem ele o servidor arranca com o
`PgVectorStore` e avisa (`make_store`).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariadne.config import Settings
from ariadne.db.models import Chunk, ChunkVector, Room
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Source as SourceRow
from ariadne.licenses import document_is_open, private_sources, record_filtered
from ariadne.semantic import PgVectorStore, SemanticStore

if TYPE_CHECKING:  # o `Services` importa este módulo
    from ariadne.services import Services

logger = logging.getLogger(__name__)

DRAWERS = "mempalace_drawers"  # o nome que o MemPalace usa para a coleção das gavetas
STATE_FILE = "ariadne-sync.json"
SAFETY_MARGIN = timedelta(
    minutes=5
)  # transações longas gravam `created_at` mais antigo que o commit
BATCH = 500


class MemPalaceError(Exception):
    """O espelho não se pôde fazer; a mensagem é para quem corre o comando."""


def mempalace_problem() -> str | None:
    """Por que o pacote não se pode usar, ou `None` se está tudo bem. Importa mesmo o que o
    espelho usa (e com ele o chromadb): instalado mas partido (binário que não corre neste CPU, por
    exemplo) também conta como problema."""
    if importlib.util.find_spec("mempalace") is None:
        return "the mempalace package is not installed (optional extra `ariadne[mempalace]`)"
    try:
        importlib.import_module("mempalace.backends.pgvector")
    except Exception as exc:  # ImportError, mas também o que um binário nativo partido levanta
        return f"the mempalace package does not start ({type(exc).__name__}: {exc})"
    return None


def mempalace_dsn(settings: Settings) -> str:
    """O Postgres onde o MemPalace guarda o espelho: `MEMPALACE_DSN`, sempre explícito. Não há
    omissão para a base do Ariadne: as tabelas do MemPalace não estão nas migrações e devem viver
    numa base própria."""
    if not settings.mempalace_dsn:
        raise MemPalaceError(
            "MEMPALACE_DSN is missing (the mirror's Postgres, preferably a database of its own, "
            "e.g. postgresql://<user>:<password>@postgres/mempalace)."
        )
    return settings.mempalace_dsn


@dataclass
class SyncReport:
    drawers: int = 0
    wings: dict[str, int] = field(default_factory=dict)  # ala -> gavetas gravadas
    full: dict[str, bool] = field(default_factory=dict)  # ala -> foi completo (não incremental)
    removed: int = 0  # gavetas órfãs apagadas do MemPalace


class MemPalaceStore(PgVectorStore):
    """`PgVectorStore` + espelho para o MemPalace. Pesquisar e escrever fazem-se nas tabelas do
    Ariadne, exatamente como no `PgVectorStore`."""

    def __init__(self, dsn: str, palace_dir: Path, *, margin: timedelta = SAFETY_MARGIN) -> None:
        self.dsn = dsn
        self.palace_dir = palace_dir
        self.margin = margin

    # --- espelho ---------------------------------------------------------------------------

    def sync(
        self,
        session: Session,
        model_id: str,
        *,
        wing: str | None = None,
        full: bool = False,
        batch: int = BATCH,
    ) -> SyncReport:
        """Grava no MemPalace as gavetas (chunks com vetor deste modelo) de cada ala. Incremental:
        só os vetores novos desde a última vez, a não ser que as salas da ala tenham mudado (os
        rótulos vão nos metadados) ou `full`. Idempotente: uma gaveta repetida é substituída."""
        if (problem := mempalace_problem()) is not None:
            raise MemPalaceError(f"MemPalace unavailable: {problem}. (uv sync --extra mempalace)")
        from mempalace.backends.base import PalaceRef
        from mempalace.backends.pgvector import PgVectorBackend

        self.palace_dir.mkdir(parents=True, exist_ok=True)
        state = self._load_state()
        backend = PgVectorBackend()
        report = SyncReport()
        try:
            collection = backend.get_collection(
                palace=PalaceRef(id=str(self.palace_dir), local_path=str(self.palace_dir)),
                collection_name=DRAWERS,
                create=True,
                options={"dsn": self.dsn},
            )
            record_filtered(session, "mempalace sync", private_sources(session))
            wings = self._wings(session, model_id, wing)
            if wing is None:  # alas que já não têm nada para espelhar (fontes `missing`, vazias)
                self._drop_gone_wings(collection, set(wings), state)
            for name in wings:
                marks = state.setdefault(name, {})
                rooms_stamp = session.scalar(
                    select(func.max(Room.updated_at)).where(
                        Room.wing == name, Room.model_id == model_id, Room.ready
                    )
                )
                since = _parse(marks.get("synced_until"))
                seen_rooms = _parse(marks.get("rooms"))
                rooms_changed = rooms_stamp is not None and (
                    seen_rooms is None or rooms_stamp > seen_rooms
                )
                everything = full or since is None or rooms_changed
                written, newest = self._push(
                    session, collection, name, model_id, None if everything else since, batch
                )
                removed = self._sweep(session, collection, name, model_id, force=everything)
                report.drawers += written
                report.removed += removed
                report.wings[name] = written
                report.full[name] = everything
                if newest is not None:
                    marks["synced_until"] = (newest - self.margin).isoformat()
                if rooms_stamp is not None:
                    marks["rooms"] = rooms_stamp.isoformat()
                self._save_state(state)
        finally:
            backend.close()
        return report

    def _wings(self, session: Session, model_id: str, wing: str | None) -> list[str]:
        stmt = (
            select(SourceRow.wing)
            .join(DocumentRow, DocumentRow.source_id == SourceRow.id)
            .join(Chunk, Chunk.document_id == DocumentRow.id)
            .join(ChunkVector, ChunkVector.chunk_id == Chunk.id)
            .where(
                ChunkVector.model_id == model_id,
                SourceRow.status == "active",
                document_is_open(),  # o espelho é uma saída: nada da classe privado vai
            )
            .distinct()
            .order_by(SourceRow.wing)
        )
        if wing is not None:
            stmt = stmt.where(SourceRow.wing == wing)
        return list(session.scalars(stmt))

    @staticmethod
    def _eligible(stmt: Any, wing: str, model_id: str) -> Any:
        """Os chunks que têm gaveta: com vetor deste modelo, de fontes ativas da ala, ainda válidos
        (`ord >= 0`)."""
        return (
            stmt.select_from(ChunkVector)
            .join(Chunk, Chunk.id == ChunkVector.chunk_id)
            .join(DocumentRow, DocumentRow.id == Chunk.document_id)
            .join(SourceRow, SourceRow.id == DocumentRow.source_id)
            .where(
                ChunkVector.model_id == model_id,
                SourceRow.wing == wing,
                SourceRow.status == "active",
                Chunk.ord >= 0,
                document_is_open(),
            )
        )

    def _sweep(
        self, session: Session, collection: Any, wing: str, model_id: str, *, force: bool
    ) -> int:
        """Apaga do MemPalace as gavetas da ala sem chunk correspondente (chunks removidos,
        documentos que encolheram, fontes que sumiram). Barato: só compara contagens, e só vai aos
        ids quando o MemPalace tem mais gavetas do que chunks (ou numa passagem completa)."""
        expected = int(session.scalar(self._eligible(select(func.count()), wing, model_id)) or 0)
        mirrored = int(collection.facet_counts("wing").get(wing, 0))
        if mirrored <= expected and not force:
            return 0
        present = collection.get(where={"wing": wing}, include=[]).ids
        wanted: set[str] = set()
        after = 0
        while True:
            rows = session.execute(
                self._eligible(
                    select(ChunkVector.chunk_id, Chunk.document_id, Chunk.ord), wing, model_id
                )
                .where(ChunkVector.chunk_id > after)
                .order_by(ChunkVector.chunk_id)
                .limit(20_000)
            ).all()
            if not rows:
                break
            wanted.update(f"{row.document_id}#{row.ord}" for row in rows)
            after = rows[-1].chunk_id
        orphans = [i for i in present if i not in wanted]
        for start in range(0, len(orphans), 1000):
            collection.delete(ids=orphans[start : start + 1000])
        return len(orphans)

    def _drop_gone_wings(
        self, collection: Any, current: set[str], state: dict[str, dict[str, str]]
    ) -> None:
        for gone in set(collection.facet_counts("wing")) - current:
            collection.delete(where={"wing": gone})
            state.pop(gone, None)
        self._save_state(state)

    def _push(
        self,
        session: Session,
        collection: Any,
        wing: str,
        model_id: str,
        since: datetime | None,
        batch: int,
    ) -> tuple[int, datetime | None]:
        written = 0
        newest: datetime | None = None
        after = 0
        now = datetime.now(UTC).isoformat()
        while True:
            stmt = (
                select(
                    ChunkVector.chunk_id,
                    ChunkVector.embedding,
                    ChunkVector.created_at,
                    Chunk.document_id,
                    Chunk.ord,
                    Chunk.text,
                    DocumentRow.title,
                    Room.id.label("room_id"),
                    Room.label.label("room_label"),
                )
                .join(Chunk, Chunk.id == ChunkVector.chunk_id)
                .join(DocumentRow, DocumentRow.id == Chunk.document_id)
                .join(SourceRow, SourceRow.id == DocumentRow.source_id)
                .outerjoin(Room, (Room.id == DocumentRow.room) & Room.ready)
                .where(
                    ChunkVector.model_id == model_id,
                    SourceRow.wing == wing,
                    SourceRow.status == "active",
                    Chunk.ord >= 0,
                    ChunkVector.chunk_id > after,
                    document_is_open(),
                )
                .order_by(ChunkVector.chunk_id)
                .limit(batch)
            )
            if since is not None:
                stmt = stmt.where(ChunkVector.created_at > since)
            rows = session.execute(stmt).all()
            if not rows:
                return written, newest
            collection.upsert(
                ids=[f"{row.document_id}#{row.ord}" for row in rows],
                documents=[row.text for row in rows],
                embeddings=[[float(x) for x in row.embedding] for row in rows],
                metadatas=[_metadata(row, wing, model_id, now) for row in rows],
            )
            written += len(rows)
            newest = max([r.created_at for r in rows] + ([newest] if newest else []))
            after = rows[-1].chunk_id

    # --- estado do espelho -----------------------------------------------------------------

    def _state_path(self) -> Path:
        return self.palace_dir / STATE_FILE

    def _load_state(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, dict[str, str]]) -> None:
        self._state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _metadata(row: Any, wing: str, model_id: str, filed_at: str) -> dict[str, Any]:
    """Os metadados de uma gaveta, com os nomes que o MemPalace usa (`wing`, `room`, …)."""
    metadata: dict[str, Any] = {
        "wing": wing,
        "source_file": row.document_id,
        "chunk_index": int(row.ord),
        "title": row.title,
        "added_by": "ariadne",
        "filed_at": filed_at,
        "ariadne_model": model_id,
    }
    if row.room_id is not None:
        metadata["room"] = row.room_label
        metadata["room_id"] = int(row.room_id)
    return metadata


_last_warning: list[str | None] = [None]


def run_mempalace_sync(
    services: Services, stop: threading.Event | None = None
) -> SyncReport | None:
    """Tarefa periódica do worker `mempalace`: espelha as gavetas novas, mas só com
    SEMANTIC_STORE=mempalace (e o pacote instalado); caso contrário não faz nada."""
    from ariadne import router  # (import à última hora: o router importa o `Services`)

    store = services.store
    if not isinstance(store, MemPalaceStore):
        return None
    model_id = router.current_model_id(services)
    if model_id is None:
        return None
    try:
        with services.sessions() as session:
            report = store.sync(session, model_id)
    except MemPalaceError as exc:  # sem pacote ou sem DSN: avisa uma vez, não a cada ronda
        if str(exc) != _last_warning[0]:
            _last_warning[0] = str(exc)
            logger.warning("MemPalace: mirror paused: %s", exc)
        return None
    _last_warning[0] = None
    if report.drawers:
        logger.info(
            "MemPalace: %d drawers mirrored, %d orphans deleted (%s)",
            report.drawers,
            report.removed,
            report.wings,
        )
    return report


@dataclass(frozen=True)
class StoreChoice:
    store: SemanticStore
    name: str  # o que está a funcionar: `pgvector` ou `mempalace`
    note: str | None = None  # por que não é o pedido, se não for


def make_store(settings: Settings) -> StoreChoice:
    """O `SemanticStore` pedido por `SEMANTIC_STORE`. `mempalace` cai para o `pgvector` (que faz
    exatamente o mesmo, sem o espelho) e diz porquê se faltar o pacote, se ele não arrancar ou se
    faltar `MEMPALACE_DSN`: nunca derruba o arranque."""
    if settings.semantic_store != "mempalace":
        return StoreChoice(PgVectorStore(), "pgvector")
    problem = mempalace_problem()
    if problem is None:
        try:
            dsn = mempalace_dsn(settings)
        except MemPalaceError:
            problem = (
                "MEMPALACE_DSN is missing (the mirror's Postgres, preferably a database of its own)"
            )
        else:
            return StoreChoice(MemPalaceStore(dsn, settings.mempalace_palace_dir), "mempalace")
    note = f"SEMANTIC_STORE=mempalace was requested, but {problem}: using pgvector."
    logger.warning(note)
    return StoreChoice(PgVectorStore(), "pgvector", note)
