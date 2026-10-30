"""Fase 9: o armazenamento do nível 3 (`SEMANTIC_STORE`), o arranque sem o extra do MemPalace e o
espelho Ariadne → MemPalace (este só corre com `ariadne[mempalace]` instalado)."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from ariadne import mempalace_store
from ariadne.app import create_app
from ariadne.config import Settings
from ariadne.db.models import Chunk, Room
from ariadne.db.models import Source as SourceRow
from ariadne.mempalace_store import (
    MemPalaceError,
    MemPalaceStore,
    make_store,
    mempalace_dsn,
    run_mempalace_sync,
)
from ariadne.semantic import PgVectorStore
from ariadne.services import Services
from tests.fakes import default_fake
from tests.test_level3 import SOURCE, make_services, put_chunk, unit_vector
from tests.test_search_rooms import MODEL, WING, make_room, put_in_room

PG = "postgresql+psycopg://x:y@localhost/z"
PROBLEM = "the mempalace package is not installed (optional extra `ariadne[mempalace]`)"
DSN = "postgresql://x:y@localhost/mempalace"


def settings(**overrides: object) -> Settings:
    return Settings(database_url=PG, **overrides)  # type: ignore[arg-type]


def test_por_omissao_e_o_pgvector() -> None:
    choice = make_store(settings())
    assert (choice.name, choice.note) == ("pgvector", None)
    assert type(choice.store) is PgVectorStore


def test_mempalace_pedido_sem_o_pacote_cai_para_pgvector_e_avisa(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mempalace_store, "mempalace_problem", lambda: PROBLEM)

    with caplog.at_level("WARNING"):
        choice = make_store(settings(semantic_store="mempalace"))

    assert (choice.name, type(choice.store)) == ("pgvector", PgVectorStore)
    assert choice.note is not None and "mempalace package is not installed" in choice.note
    assert "SEMANTIC_STORE=mempalace" in caplog.text


def test_mempalace_pedido_com_o_pacote_usa_o_adaptador(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mempalace_store, "mempalace_problem", lambda: None)

    choice = make_store(
        settings(semantic_store="mempalace", mempalace_dsn=DSN, mempalace_palace_dir=tmp_path)
    )

    assert (choice.name, choice.note) == ("mempalace", None)
    assert isinstance(choice.store, MemPalaceStore)
    assert isinstance(choice.store, PgVectorStore)  # pesquisa e escrita são as das tabelas nossas
    assert choice.store.palace_dir == tmp_path


def test_o_dsn_do_mempalace_e_sempre_explicito() -> None:
    assert mempalace_dsn(settings(mempalace_dsn="postgresql://a/b")) == "postgresql://a/b"
    with pytest.raises(MemPalaceError, match="MEMPALACE_DSN is missing"):  # nunca a base do Ariadne
        mempalace_dsn(settings())


def test_mempalace_pedido_sem_dsn_cai_para_pgvector_e_avisa(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mempalace_store, "mempalace_problem", lambda: None)

    with caplog.at_level("WARNING"):
        choice = make_store(settings(semantic_store="mempalace"))

    assert (choice.name, type(choice.store)) == ("pgvector", PgVectorStore)
    assert choice.note is not None and "MEMPALACE_DSN is missing" in choice.note
    assert "MEMPALACE_DSN" in caplog.text


@pytest.mark.parametrize("error", [ImportError("sem libchroma"), RuntimeError("CPU sem AVX")])
def test_pacote_instalado_mas_que_nao_arranca_conta_como_problema(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(mempalace_store.importlib.util, "find_spec", lambda name: object())

    def broken(name: str) -> None:
        raise error

    monkeypatch.setattr(mempalace_store.importlib, "import_module", broken)

    problem = mempalace_store.mempalace_problem()
    assert problem is not None and "does not start" in problem and str(error) in problem
    choice = make_store(settings(semantic_store="mempalace", mempalace_dsn=DSN))
    assert (choice.name, type(choice.store)) == ("pgvector", PgVectorStore)
    assert choice.note is not None and "does not start" in choice.note


def test_a_tarefa_do_worker_avisa_uma_vez_e_nao_falha_a_cada_ronda(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Gone:
        def sync(self, session: object, model_id: str) -> None:
            raise MemPalaceError("sem pacote")

    class Sessions:
        def __call__(self) -> "Sessions":
            return self

        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            return None

    store = MemPalaceStore(DSN, Path("/nao/usado"))
    monkeypatch.setattr(store, "sync", Gone().sync)
    services = Services.__new__(Services)  # só precisa de `store`, `sessions` e o router
    services.store = store
    services.sessions = Sessions()  # type: ignore[assignment]
    monkeypatch.setattr("ariadne.router.current_model_id", lambda s: MODEL)
    mempalace_store._last_warning[0] = None

    with caplog.at_level("WARNING"):
        assert [run_mempalace_sync(services) for _ in range(3)] == [None, None, None]

    assert caplog.text.count("mirror paused") == 1


def test_sync_sem_o_pacote_explica_o_que_falta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mempalace_store, "mempalace_problem", lambda: PROBLEM)
    store = MemPalaceStore("postgresql://x/y", tmp_path)
    with pytest.raises(MemPalaceError, match="uv sync --extra mempalace"):
        store.sync(None, MODEL)  # type: ignore[arg-type]


@pytest.mark.db
def test_health_diz_o_armazenamento_e_porque_cai_para_o_pgvector(
    monkeypatch: pytest.MonkeyPatch, database_url: str, zim_dir: Path
) -> None:
    monkeypatch.setattr(mempalace_store, "mempalace_problem", lambda: PROBLEM)
    config = Settings(
        database_url=database_url,
        zim_dir=zim_dir,
        kiwix_url="http://kiwix.invalido",
        semantic_store="mempalace",
    )

    with TestClient(create_app(config)) as client:
        body = client.get("/health").json()
        services: Services = client.app.state.services  # type: ignore[attr-defined]

    assert type(services.store) is PgVectorStore
    assert body["checks"]["semantic_store"].startswith("pgvector (SEMANTIC_STORE=mempalace")
    assert "is not installed" in body["checks"]["semantic_store"]


@pytest.mark.db
def test_health_com_o_armazenamento_por_omissao(database_url: str, zim_dir: Path) -> None:
    config = Settings(database_url=database_url, zim_dir=zim_dir, kiwix_url="http://kiwix.invalido")
    with TestClient(create_app(config)) as client:
        assert client.get("/health").json()["checks"]["semantic_store"] == "pgvector"


@pytest.mark.db
def test_a_tarefa_do_worker_nao_faz_nada_com_o_pgvector(
    db: sessionmaker[Session], zim_dir: Path
) -> None:
    from ariadne.embedder import FakeEmbedder

    services = make_services(db, zim_dir, default_fake(), FakeEmbedder())
    try:
        assert run_mempalace_sync(services) is None
    finally:
        services.close()


# --- com o pacote instalado: `uv run --extra mempalace pytest -m mempalace` ---


@pytest.fixture
def mp_services(db: sessionmaker[Session], zim_dir: Path, tmp_path: Path) -> Iterator[Services]:
    pytest.importorskip("mempalace")
    from ariadne.embedder import FakeEmbedder

    services = make_services(db, zim_dir, default_fake(), FakeEmbedder())
    services.store = MemPalaceStore(
        _url(db).replace("postgresql+psycopg", "postgresql"),
        tmp_path / "palacio",
        margin=timedelta(0),  # (em produção há uma margem para transações longas)
    )
    yield services
    services.close()


def _url(db: sessionmaker[Session]) -> str:
    bind = db.kw["bind"]
    return bind.url.render_as_string(hide_password=False)  # type: ignore[no-any-return]


def drawers(store: MemPalaceStore) -> dict[str, dict[str, object]]:
    from mempalace.backends.base import PalaceRef
    from mempalace.backends.pgvector import PgVectorBackend

    backend = PgVectorBackend()
    try:
        collection = backend.get_collection(
            palace=PalaceRef(id=str(store.palace_dir), local_path=str(store.palace_dir)),
            collection_name=mempalace_store.DRAWERS,
            create=False,
            options={"dsn": store.dsn},
        )
        found = collection.get(include=["metadatas", "documents"])
        return {
            id_: {**meta, "document": doc}
            for id_, meta, doc in zip(found.ids, found.metadatas, found.documents, strict=True)
        }
    finally:
        backend.close()


@pytest.mark.db
@pytest.mark.mempalace
def test_sync_apaga_gavetas_orfas_e_a_palavra_passe_nao_fica_no_palacio(
    mp_services: Services, tmp_path: Path
) -> None:
    services = mp_services
    store = services.store
    assert isinstance(store, MemPalaceStore)
    with services.sessions() as session:
        put_chunk(services, session, "Lisboa", 0.92)
        put_chunk(services, session, "Portugal", 0.5)
        put_chunk(services, session, "Outro", 0.4)
        assert store.sync(session, MODEL).drawers == 3

        # Um chunk deixa de valer (o documento encolheu) e outro documento sai da fonte.
        session.execute(update(Chunk).where(Chunk.document_id == f"{SOURCE}/Lisboa").values(ord=-1))
        session.commit()
        incremental = store.sync(session, MODEL)
        assert incremental.removed == 1
        assert sorted(drawers(store)) == [f"{SOURCE}/Outro#0", f"{SOURCE}/Portugal#0"]

        session.execute(update(SourceRow).where(SourceRow.id == SOURCE).values(status="missing"))
        session.commit()
        gone = store.sync(session, MODEL)
        assert (gone.removed, gone.drawers) == (0, 0)  # (nada ativo: a ala sai do MemPalace)
        assert drawers(store) == {}

    # A palavra-passe do DSN nunca vai para o que o MemPalace grava em disco.
    secret = make_url(store.dsn).password
    assert secret
    for file in (tmp_path / "palacio").rglob("*"):
        if file.is_file():
            assert secret not in file.read_text(encoding="utf-8", errors="ignore"), file


@pytest.mark.db
@pytest.mark.mempalace
def test_sync_espelha_chunks_com_ala_e_sala_e_e_incremental(mp_services: Services) -> None:
    services = mp_services
    store = services.store
    assert isinstance(store, MemPalaceStore)
    with services.sessions() as session:
        put_chunk(services, session, "Lisboa", 0.92)
        put_chunk(services, session, "Portugal", 0.5)
        sala = make_room(session, "Cidades · Países", unit_vector(1.0))
        put_in_room(services, session, sala, "Lisboa")

        first = store.sync(session, MODEL)

        assert (first.drawers, first.wings, first.full) == (
            2,
            {WING: 2},
            {WING: True},
        )
        got = drawers(store)
        lisboa = got[f"{SOURCE}/Lisboa#0"]
        assert (lisboa["wing"], lisboa["room"], lisboa["room_id"]) == (
            WING,
            "Cidades · Países",
            sala,
        )
        assert lisboa["document"] == "Lisboa — texto de Lisboa"
        assert lisboa["source_file"] == f"{SOURCE}/Lisboa" and lisboa["added_by"] == "ariadne"
        assert "room" not in got[f"{SOURCE}/Portugal#0"]  # sem sala ainda

        # Nada de novo: incremental, zero gavetas.
        again = store.sync(session, MODEL)
        assert (again.drawers, again.full) == (0, {WING: False})

        # Um chunk novo: só ele vai.
        put_chunk(services, session, "Outro", 0.4)
        third = store.sync(session, MODEL)
        assert (third.drawers, third.full) == (1, {WING: False})
        assert len(drawers(store)) == 3

        # As salas mudaram (rótulo novo): reenvia a ala toda, com o rótulo atualizado.
        room = session.get_one(Room, sala)
        room.label = "Capitais"
        room.updated_at = func.now()
        session.commit()
        fourth = store.sync(session, MODEL)
        assert (fourth.drawers, fourth.full) == (3, {WING: True})
        assert drawers(store)[f"{SOURCE}/Lisboa#0"]["room"] == "Capitais"
        assert store.sync(session, MODEL, full=True).drawers == 3
        assert store.sync(session, MODEL, wing="outra").drawers == 0
        # A fonte de verdade não mudou: continua a pesquisar nas tabelas do Ariadne.
        assert session.scalar(select(func.count()).select_from(Room)) == 1
