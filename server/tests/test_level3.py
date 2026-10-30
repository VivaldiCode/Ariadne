"""Nível 3: indexador, router (limiares), fila de indexação e calor, com o FakeEmbedder."""

import math
import random
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from ariadne import heat, queue
from ariadne.auth import PrincipalKind, create_principal
from ariadne.config import Settings
from ariadne.db.models import Chunk, ChunkVector, Job, QueryLog, QueryResult
from ariadne.db.models import Document as DocumentRow
from ariadne.documents import DocumentInfo, read_document, upsert_documents
from ariadne.embedder import EMBEDDING_DIM, FakeEmbedder
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.search import give_feedback, search
from ariadne.services import Services
from ariadne.sources.registry import SourceRegistry
from ariadne.workers import indexer
from ariadne.workers.decay import run_decay
from ariadne.workers.runner import Runner, UnknownWorker, parse_workers
from tests.fakes import PT_BOOK, FakeKiwix, default_fake

pytestmark = pytest.mark.db

SOURCE = "zim:wikipedia_pt_teste"
LISBOA = f"{SOURCE}/Lisboa"
PORTUGAL = f"{SOURCE}/Portugal"
OUTRO = f"{SOURCE}/Outro"
QUERY = "capital"


def unit_vector(similarity: float, axis: int = 1) -> list[float]:
    """Vetor com a semelhança cosseno dada ao vetor da pergunta (o eixo 0)."""
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = similarity
    vector[axis] = math.sqrt(1 - similarity**2)
    return vector


QUERY_VECTOR = unit_vector(1.0)


@pytest.fixture
def fake() -> FakeKiwix:
    return default_fake()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(vectors={QUERY: QUERY_VECTOR})


def make_services(
    sessions: sessionmaker[Session],
    zim_dir: Path,
    kiwix: FakeKiwix,
    embedder: FakeEmbedder,
    **overrides: object,
) -> Services:
    settings = Settings(
        database_url="postgresql+psycopg://x@localhost/x",
        zim_dir=zim_dir,
        kiwix_url="http://kiwix",
        kiwix_public_url="http://kiwix.publico:8801",
        **overrides,  # type: ignore[arg-type]
    )
    registry = SourceRegistry(zim_dir)
    with sessions() as session:
        registry.sync(session)
    return Services(settings, sessions, registry, kiwix.client(), embedder=embedder)


@pytest.fixture
def services(
    db: sessionmaker[Session], zim_dir: Path, fake: FakeKiwix, embedder: FakeEmbedder
) -> Iterator[Services]:
    services = make_services(db, zim_dir, fake, embedder)
    yield services
    services.close()


@pytest.fixture
def principal_id(db: sessionmaker[Session]) -> int:
    with db() as session, session.begin():
        principal, _ = create_principal(session, PrincipalKind.agent, "agente-de-teste")
        return principal.id


@pytest.fixture
def session(db: sessionmaker[Session]) -> Iterator[Session]:
    with db() as s:
        yield s


def make_document(
    services: Services, session: Session, path: str, title: str | None = None
) -> None:
    source = services.registry.get(SOURCE)
    assert source is not None
    upsert_documents(session, [DocumentInfo(source, path, title or path)])
    session.commit()


def put_chunk(
    services: Services, session: Session, path: str, similarity: float, *, title: str | None = None
) -> int:
    """Um documento com um chunk cujo vetor tem a semelhança dada à pergunta."""
    title = title or path
    make_document(services, session, path, title)
    document_id = f"{SOURCE}/{path}"
    services.store.put_chunks(session, document_id, [f"{title} — texto de {path}"])
    (pending,) = services.store.missing_vectors(session, document_id, "fake/embedder@0000000")
    services.store.put_vectors(
        session, "fake/embedder@0000000", {pending.chunk_id: unit_vector(similarity)}
    )
    session.commit()
    return pending.chunk_id


# --- router: os três ramos dos limiares ------------------------------------------------------


def test_semelhanca_alta_responde_so_com_o_nivel_3(
    services: Services, session: Session, principal_id: int, fake: FakeKiwix
) -> None:
    chunk_id = put_chunk(services, session, "Lisboa", 0.92)

    result = search(services, session, principal_id, QUERY, k=1)

    assert fake.requests == []  # o kiwix nem foi consultado
    (hit,) = result.results
    assert (hit.document_id, hit.level, hit.chunk_id) == (LISBOA, 3, chunk_id)
    assert hit.score == pytest.approx(0.92, abs=1e-3)
    assert hit.snippet == "texto de Lisboa"  # sem o «Título — » do chunk
    assert hit.kiwix_url == f"http://kiwix.publico:8801/content/{PT_BOOK}/Lisboa"
    log = session.get(QueryLog, result.query_id)
    assert log is not None and log.level_served == 3
    row = session.scalars(select(QueryResult)).one()
    assert (row.chunk_id, row.level, row.document_id) == (chunk_id, 3, LISBOA)
    assert session.scalars(select(Job)).all() == []  # nada veio do fallback: nada a indexar


def test_nivel_3_que_nao_enche_a_pagina_completa_com_o_fallback(
    services: Services, session: Session, principal_id: int, fake: FakeKiwix
) -> None:
    chunk_id = put_chunk(services, session, "Lisboa", 0.92)

    result = search(services, session, principal_id, QUERY, k=3)

    assert len(fake.requests) == 1
    assert [(h.document_id, h.level) for h in result.results] == [
        (LISBOA, 3),
        (PORTUGAL, 1),
        (OUTRO, 1),
    ]  # o do nível 3 primeiro; o fallback só preenche (sem repetir Lisboa)
    assert result.results[0].chunk_id == chunk_id
    assert result.results[0].score == pytest.approx(0.92, abs=1e-3)
    queued = {j.payload["document_id"] for j in session.scalars(select(Job))}
    assert queued == {PORTUGAL, OUTRO}  # o que veio do fallback entra na fila


def test_zona_cinzenta_funde_nivel_3_e_fallback(
    services: Services, session: Session, principal_id: int, fake: FakeKiwix
) -> None:
    put_chunk(services, session, "Outro", 0.83)  # entre τ_baixo (0,80) e τ_alto (0,86)

    result = search(services, session, principal_id, QUERY)

    assert len(fake.requests) == 1
    by_id = {h.document_id: h for h in result.results}
    assert set(by_id) == {LISBOA, PORTUGAL, OUTRO}
    assert by_id[OUTRO].level == 3 and by_id[OUTRO].chunk_id is not None
    assert by_id[LISBOA].level == by_id[PORTUGAL].level == 1
    assert result.results[0].document_id == OUTRO  # está nas duas listas: RRF soma
    top = result.results[0].score
    assert top > 1 / 61  # mais do que uma única lista dá
    log = session.get(QueryLog, result.query_id)
    assert log is not None and log.level_served == 3


def test_zona_cinzenta_ordem_do_fallback_segue_o_embedding(
    db: sessionmaker[Session],
    zim_dir: Path,
    fake: FakeKiwix,
    principal_id: int,
    session: Session,
) -> None:
    # O kiwix dá Lisboa, Portugal, Outro; o embedding de «título. excerto» prefere o Outro.
    embedder = FakeEmbedder(
        vectors={
            QUERY: QUERY_VECTOR,
            "Lisboa. excerto de Lisboa": unit_vector(0.2),
            "Portugal. excerto de Portugal": unit_vector(0.5),
            "Outro. excerto de Outro": unit_vector(0.9),
        }
    )
    services = make_services(db, zim_dir, fake, embedder)
    try:
        put_chunk(services, session, "Chunk_isolado", 0.82)
        result = search(services, session, principal_id, QUERY)
    finally:
        services.close()

    fallback_order = [h.document_id for h in result.results if h.level == 1]
    assert fallback_order == [OUTRO, PORTUGAL, LISBOA]
    assert ("passage", ["Lisboa. excerto de Lisboa", "Portugal. excerto de Portugal",
                        "Outro. excerto de Outro"]) in embedder.calls  # fmt: skip


def test_semelhanca_baixa_so_usa_o_fallback(
    services: Services, session: Session, principal_id: int, fake: FakeKiwix
) -> None:
    put_chunk(services, session, "Lisboa", 0.70)  # parecido, mas abaixo de τ_baixo

    result = search(services, session, principal_id, QUERY)

    assert [h.level for h in result.results] == [1, 1, 1]
    assert all(h.chunk_id is None for h in result.results)
    assert result.results[0].score == 1.0  # a pontuação do kiwix, sem fusão
    assert len(fake.requests) == 1


def test_sem_vetores_usa_o_fallback(
    services: Services, session: Session, principal_id: int
) -> None:
    result = search(services, session, principal_id, QUERY)
    assert [h.level for h in result.results] == [1, 1, 1]


def test_limiares_vem_da_configuracao(
    db: sessionmaker[Session],
    zim_dir: Path,
    fake: FakeKiwix,
    embedder: FakeEmbedder,
    session: Session,
    principal_id: int,
) -> None:
    services = make_services(db, zim_dir, fake, embedder, router_tau_high=0.7, router_tau_low=0.6)
    try:
        put_chunk(services, session, "Lisboa", 0.75)
        result = search(services, session, principal_id, QUERY, k=1)
    finally:
        services.close()
    assert [h.level for h in result.results] == [3]
    assert fake.requests == []


def test_embedder_em_baixo_degrada_para_o_nivel_1(
    services: Services, session: Session, principal_id: int, embedder: FakeEmbedder
) -> None:
    put_chunk(services, session, "Lisboa", 0.95)
    embedder.failing = True

    result = search(services, session, principal_id, QUERY)

    assert [h.level for h in result.results] == [1, 1, 1]
    assert any("Semantic search unavailable" in w for w in result.warnings)


def test_filtro_de_fonte_e_de_lingua_chega_ao_nivel_3(
    services: Services, session: Session, principal_id: int, fake: FakeKiwix
) -> None:
    put_chunk(services, session, "Lisboa", 0.95)
    assert search(services, session, principal_id, QUERY, lang="pt", k=1).results[0].level == 3
    assert (
        search(services, session, principal_id, QUERY, sources=[SOURCE], k=1).results[0].level == 3
    )
    assert fake.requests == []


def test_documentos_repetidos_no_nivel_3_ficam_so_com_o_melhor_chunk(
    services: Services, session: Session, principal_id: int
) -> None:
    first = put_chunk(services, session, "Lisboa", 0.97)
    services.store.put_chunks(session, LISBOA, ["Lisboa — texto de Lisboa", "Lisboa — outro"])
    (second,) = services.store.missing_vectors(session, LISBOA, "fake/embedder@0000000")
    services.store.put_vectors(
        session, "fake/embedder@0000000", {second.chunk_id: unit_vector(0.9)}
    )
    session.commit()

    result = search(services, session, principal_id, QUERY, k=1)

    assert [(h.document_id, h.chunk_id) for h in result.results] == [(LISBOA, first)]


def test_zona_cinzenta_sem_kiwix_responde_com_o_que_o_nivel_3_tem(
    db: sessionmaker[Session],
    zim_dir: Path,
    fake: FakeKiwix,
    embedder: FakeEmbedder,
    session: Session,
    principal_id: int,
) -> None:
    fake.down.add(PT_BOOK)
    services = make_services(db, zim_dir, fake, embedder)
    try:
        put_chunk(services, session, "Lisboa", 0.83)
        result = search(services, session, principal_id, QUERY)
    finally:
        services.close()
    assert [(h.document_id, h.level) for h in result.results] == [(LISBOA, 3)]
    assert result.warnings


# --- fila de indexação e calor no search -----------------------------------------------------


def test_o_fallback_enfileira_os_documentos_com_prioridade_pelo_calor(
    services: Services, session: Session, principal_id: int
) -> None:
    search(services, session, principal_id, QUERY)
    search(services, session, principal_id, QUERY)  # repetida: não duplica, só reforça

    jobs = session.scalars(select(Job).order_by(Job.id)).all()
    assert sorted(j.payload["document_id"] for j in jobs) == [LISBOA, OUTRO, PORTUGAL]
    assert {j.kind for j in jobs} == {"index_document"} and {j.status for j in jobs} == {"pending"}
    assert all(j.dedupe_key == f"index_document:{j.payload['document_id']}" for j in jobs)
    # 0,3 por cada vez mostrado; a prioridade é o calor do documento.
    assert all(j.priority == pytest.approx(0.6, abs=1e-3) for j in jobs)
    documents = session.scalars(select(DocumentRow)).all()
    assert all(d.heat == pytest.approx(0.6, abs=1e-3) for d in documents)


def test_documento_ja_indexado_nao_volta_a_ser_enfileirado(
    services: Services, session: Session, principal_id: int
) -> None:
    search(services, session, principal_id, QUERY)
    session.execute(Job.__table__.delete())
    services.store.mark_indexed(session, LISBOA, "fake/embedder@0000000", "2026-09")
    services.store.mark_indexed(session, OUTRO, "fake/embedder@outro", "2026-09")  # outro modelo
    session.commit()

    search(services, session, principal_id, QUERY)

    queued = {j.payload["document_id"] for j in session.scalars(select(Job))}
    assert queued == {PORTUGAL, OUTRO}


def test_versao_nova_da_fonte_volta_a_pedir_indexacao(services: Services, session: Session) -> None:
    make_document(services, session, "Lisboa")
    services.store.mark_indexed(session, LISBOA, "m", "2026-09")
    assert services.store.indexed(session, [LISBOA], "m") == {LISBOA}
    session.execute(update(DocumentRow).values(version="2026-10"))
    assert services.store.indexed(session, [LISBOA], "m") == set()


def test_calor_de_mostrar_usar_e_feedback(
    services: Services, session: Session, principal_id: int
) -> None:
    chunk_id = put_chunk(services, session, "Lisboa", 0.95)
    result = search(services, session, principal_id, QUERY)  # mostrado: +0,3 (documento e chunk)

    def heats() -> tuple[float, float]:
        session.expire_all()
        document = session.get_one(DocumentRow, LISBOA)
        chunk = session.get_one(Chunk, chunk_id)
        return document.heat, chunk.heat

    assert heats() == (pytest.approx(0.3, abs=1e-3), pytest.approx(0.3, abs=1e-3))

    read_document(services, session, principal_id, LISBOA)  # usado: +1,0 (e o chunk do resultado)
    assert heats() == (pytest.approx(1.3, abs=1e-3), pytest.approx(1.3, abs=1e-3))

    give_feedback(session, principal_id, result.query_id, LISBOA, True)  # +2,0
    assert heats() == (pytest.approx(3.3, abs=1e-3), pytest.approx(3.3, abs=1e-3))

    for _ in range(3):  # repetir o feedback não soma outra vez
        give_feedback(session, principal_id, result.query_id, LISBOA, True)
    assert heats() == (pytest.approx(3.3, abs=1e-3), pytest.approx(3.3, abs=1e-3))

    give_feedback(session, principal_id, result.query_id, LISBOA, False)  # negativo: nada
    give_feedback(session, principal_id, result.query_id, LISBOA, True)  # e voltar atrás também não
    assert heats() == (pytest.approx(3.3, abs=1e-3), pytest.approx(3.3, abs=1e-3))


def test_decay_compacta_sem_mudar_a_leitura(
    services: Services, session: Session, principal_id: int
) -> None:
    search(services, session, principal_id, QUERY)
    session.execute(
        update(DocumentRow).values(heat_at=datetime.now(UTC) - timedelta(days=30), heat=8.0)
    )
    session.commit()
    before = session.scalars(select(heat.document_heat(30.0))).first()

    documents, chunks = run_decay(services)

    assert (documents, chunks) == (3, 0)
    assert session.scalars(select(DocumentRow.heat)).first() == pytest.approx(4.0, rel=1e-3)
    assert session.scalars(select(heat.document_heat(30.0))).first() == pytest.approx(
        before, rel=1e-3
    )


# --- indexador -------------------------------------------------------------------------------


def job_for(session: Session, document_id: str) -> ClaimedJob:
    job_id = indexer.enqueue_index(session, document_id, 1.0)
    session.commit()
    job = queue.claim(session, [indexer.KIND], "teste")
    session.commit()
    assert job is not None and job.id == job_id
    return job


def vectors_of(session: Session, document_id: str) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(ChunkVector)
            .join(Chunk, Chunk.id == ChunkVector.chunk_id)
            .where(Chunk.document_id == document_id)
        )
        or 0
    )


def test_indexador_cria_chunks_e_vetores_de_forma_idempotente(
    services: Services, session: Session, embedder: FakeEmbedder
) -> None:
    indexer.index_document(services, job_for(session, LISBOA))

    chunks = session.scalars(select(Chunk).where(Chunk.document_id == LISBOA)).all()
    assert len(chunks) >= 1
    assert all(c.text.startswith("Lisboa — ") for c in chunks)
    assert vectors_of(session, LISBOA) == len(chunks)
    assert services.store.indexed(session, [LISBOA], "fake/embedder@0000000") == {LISBOA}
    document = session.get_one(DocumentRow, LISBOA)
    assert document.title == "Lisboa"
    embedded = sum(len(texts) for _, texts in embedder.calls)
    assert embedded == len(chunks)

    # Segunda vez: nada de novo para vetorizar.
    indexer.index_document(services, job_for(session, LISBOA))
    assert sum(len(texts) for _, texts in embedder.calls) == embedded
    assert len(session.scalars(select(Chunk).where(Chunk.document_id == LISBOA)).all()) == len(
        chunks
    )


def test_indexador_so_vetoriza_os_chunks_que_faltam_e_troca_os_alterados(
    services: Services, session: Session
) -> None:
    make_document(services, session, "Lisboa")
    services.store.put_chunks(session, LISBOA, ["a", "b", "c"])
    pending = services.store.missing_vectors(session, LISBOA, "m")
    services.store.put_vectors(session, "m", {p.chunk_id: unit_vector(0.5) for p in pending})
    session.commit()
    assert services.store.missing_vectors(session, LISBOA, "m") == []

    services.store.put_chunks(session, LISBOA, ["a", "B", "c"])  # só o do meio mudou
    assert [p.text for p in services.store.missing_vectors(session, LISBOA, "m")] == ["B"]

    services.store.put_chunks(session, LISBOA, ["a"])  # o documento encolheu
    assert services.store.missing_vectors(session, LISBOA, "m") == []
    assert vectors_of(session, LISBOA) == 1  # os chunks a mais deixaram de ser pesquisáveis
    services.store.put_chunks(session, LISBOA, ["a", "b"])  # volta a crescer: chunks novos
    assert [p.text for p in services.store.missing_vectors(session, LISBOA, "m")] == ["b"]


def test_indexador_segue_redirects_para_o_id_canonico(services: Services, session: Session) -> None:
    indexer.index_document(services, job_for(session, f"{SOURCE}/Capital_de_Portugal"))
    assert vectors_of(session, LISBOA) >= 1
    assert services.store.indexed(session, [LISBOA], "fake/embedder@0000000") == {LISBOA}


def test_indexador_falha_de_vez_se_o_documento_nao_existe(
    services: Services, session: Session
) -> None:
    for bad in (f"{SOURCE}/NaoExiste", "zim:outra/Coisa", "sem-barra"):
        with pytest.raises(Permanent):
            indexer.index_document(services, job_for(session, bad))
    with pytest.raises(Permanent):
        indexer.index_document(services, ClaimedJob(1, indexer.KIND, {}, 1, 0.0))


def test_orcamento_diario_adia_o_job_para_amanha_e_retoma(
    db: sessionmaker[Session],
    zim_dir: Path,
    fake: FakeKiwix,
    embedder: FakeEmbedder,
    session: Session,
) -> None:
    services = make_services(
        db, zim_dir, fake, embedder, chunk_words=10, index_daily_budget=2, embedder_batch=1
    )
    model_id = "fake/embedder@0000000"
    try:
        job = job_for(session, LISBOA)
        with pytest.raises(Defer) as later:
            indexer.index_document(services, job)
        total = len(session.scalars(select(Chunk).where(Chunk.document_id == LISBOA)).all())
        assert total > 2  # o documento não cabe no orçamento de um dia
        assert vectors_of(session, LISBOA) == 2
        assert services.store.indexed(session, [LISBOA], model_id) == set()
        tomorrow = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        assert later.value.run_after == tomorrow + timedelta(days=1)

        # No dia seguinte (os vetores de hoje passam a ser «de ontem») retoma-se de onde ficou.
        for expected in range(4, total + 3, 2):
            session.execute(
                update(ChunkVector).values(created_at=datetime.now(UTC) - timedelta(days=1))
            )
            session.commit()
            try:
                indexer.index_document(services, job)
            except Defer:
                assert vectors_of(session, LISBOA) == expected
            else:
                break
        assert vectors_of(session, LISBOA) == total
        assert services.store.indexed(session, [LISBOA], model_id) == {LISBOA}
    finally:
        services.close()


def test_orcamento_conta_so_os_vetores_de_hoje_do_modelo_atual(
    services: Services, session: Session
) -> None:
    put_chunk(services, session, "Outro", 0.5)
    services.store.put_chunks(session, OUTRO, ["Outro — texto de Outro", "x"])
    (pending,) = services.store.missing_vectors(session, OUTRO, "outro-modelo")[1:]
    services.store.put_vectors(session, "outro-modelo", {pending.chunk_id: unit_vector(0.1)})
    session.commit()
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert services.store.count_vectors_since(session, "fake/embedder@0000000", midnight) == 1
    assert services.store.count_vectors_since(session, "outro-modelo", midnight) == 1
    assert (
        services.store.count_vectors_since(
            session, "fake/embedder@0000000", midnight + timedelta(days=1)
        )
        == 0
    )


# --- o ciclo do worker -----------------------------------------------------------------------


def test_runner_trata_a_fila_do_principio_ao_fim(
    services: Services, session: Session, principal_id: int
) -> None:
    search(services, session, principal_id, QUERY)  # enfileira Lisboa, Portugal e Outro
    runner = Runner(services, ["indexer"], worker_id="teste")

    handled = 0
    while runner.run_once():
        handled += 1
    assert handled == 6  # 3 indexações e, depois de cada uma, o vetor de artigo
    session.expire_all()
    assert {j.status for j in session.scalars(select(Job))} == {"done"}
    assert {j.kind for j in session.scalars(select(Job))} == {"index_document", "article_vector"}
    assert services.store.has_article_vectors(
        session, [LISBOA, PORTUGAL, OUTRO], "fake/embedder@0000000"
    ) == {LISBOA, PORTUGAL, OUTRO}
    assert services.store.indexed(session, [LISBOA, PORTUGAL, OUTRO], "fake/embedder@0000000") == {
        LISBOA,
        PORTUGAL,
        OUTRO,
    }

    # A pergunta repetida já é servida pelo nível 3 (com este embedder o texto da pergunta
    # não se parece com os chunks: a prova com o modelo real está no teste `model`).
    assert not runner.run_once()


def test_runner_falha_de_vez_quando_o_documento_nao_existe(
    services: Services, session: Session
) -> None:
    runner = Runner(services, ["indexer"], worker_id="teste")
    queue.enqueue(session, indexer.KIND, {"document_id": "zim:outra/X"}, priority=2)
    session.commit()
    assert runner.run_once()
    session.expire_all()
    permanent = session.scalars(select(Job)).one()
    assert permanent.status == "failed" and "Unknown source" in (permanent.last_error or "")


def test_embedder_em_baixo_adia_o_job_sem_gastar_tentativas(
    services: Services, session: Session
) -> None:
    runner = Runner(services, ["indexer"], worker_id="teste")
    queue.enqueue(session, indexer.KIND, {"document_id": LISBOA})
    session.commit()
    services.embedder = FakeEmbedder()
    services.embedder.failing = True
    for _ in range(7):  # mais do que JOB_MAX_ATTEMPTS: nunca acaba `failed`
        assert runner.run_once()
        session.execute(update(Job).values(run_after=datetime.now(UTC) - timedelta(minutes=1)))
        session.commit()
    session.expire_all()
    job = session.scalars(select(Job)).one()
    assert (job.status, job.attempts) == ("pending", 0)
    assert "unavailable" in (job.last_error or "")


def test_paragem_entre_lotes_devolve_o_job_sem_gastar_tentativa(
    db: sessionmaker[Session],
    zim_dir: Path,
    fake: FakeKiwix,
    embedder: FakeEmbedder,
    session: Session,
) -> None:
    services = make_services(db, zim_dir, fake, embedder, chunk_words=10, embedder_batch=1)
    try:
        queue.enqueue(session, indexer.KIND, {"document_id": LISBOA})
        session.commit()
        runner = Runner(services, ["indexer"], worker_id="teste")
        original = embedder.embed_passages

        def stop_after_first(texts: Sequence[str]) -> list[list[float]]:
            runner.stop.set()  # SIGTERM chega a meio do documento
            return original(texts)

        embedder.embed_passages = stop_after_first  # type: ignore[method-assign]
        assert runner.run_once()
        session.expire_all()
        job = session.scalars(select(Job)).one()
        assert (job.status, job.attempts) == ("pending", 0)
        assert vectors_of(session, LISBOA) == 1  # só o lote que já ia a meio
    finally:
        services.close()


def test_redirect_fica_marcado_como_indexado_com_os_dois_ids(
    services: Services, session: Session
) -> None:
    redirect = f"{SOURCE}/Capital_de_Portugal"
    make_document(services, session, "Capital_de_Portugal", "Capital de Portugal")
    indexer.index_document(services, job_for(session, redirect))
    model = "fake/embedder@0000000"
    assert services.store.indexed(session, [LISBOA, redirect], model) == {LISBOA, redirect}


def test_documento_com_falha_recente_nao_volta_a_ser_enfileirado(
    services: Services, session: Session, principal_id: int
) -> None:
    search(services, session, principal_id, QUERY)
    session.execute(
        update(Job)
        .where(Job.dedupe_key == indexer.dedupe_key(OUTRO))
        .values(status="failed", finished_at=datetime.now(UTC), last_error="não é artigo")
    )
    session.execute(  # a falha do Portugal é antiga: pode tentar-se de novo
        update(Job)
        .where(Job.dedupe_key == indexer.dedupe_key(PORTUGAL))
        .values(status="failed", finished_at=datetime.now(UTC) - timedelta(days=30))
    )
    session.commit()

    search(services, session, principal_id, QUERY)

    pending = {
        j.payload["document_id"] for j in session.scalars(select(Job)) if j.status == "pending"
    }
    assert OUTRO not in pending and PORTUGAL in pending


def test_runner_adia_sem_gastar_tentativas(
    db: sessionmaker[Session],
    zim_dir: Path,
    fake: FakeKiwix,
    embedder: FakeEmbedder,
    session: Session,
) -> None:
    services = make_services(db, zim_dir, fake, embedder, index_daily_budget=0)
    try:
        queue.enqueue(session, indexer.KIND, {"document_id": LISBOA})
        session.commit()
        assert Runner(services, ["indexer"], worker_id="teste").run_once()
        session.expire_all()
        job = session.scalars(select(Job)).one()
        assert (job.status, job.attempts) == ("pending", 0)
        assert job.run_after > datetime.now(UTC)
    finally:
        services.close()


def test_orcamento_a_zero_adia_sem_abrir_o_zim(services_zero: Services, session: Session) -> None:
    # Se lesse o artigo, a fonte desconhecida dava `Permanent`; com o orçamento a zero adia já.
    with pytest.raises(Defer):
        indexer.index_document(services_zero, job_for(session, "zim:outra/Coisa"))


@pytest.fixture
def services_zero(
    db: sessionmaker[Session], zim_dir: Path, fake: FakeKiwix, embedder: FakeEmbedder
) -> Iterator[Services]:
    services = make_services(db, zim_dir, fake, embedder, index_daily_budget=0)
    yield services
    services.close()


def test_runner_arranca_para_e_recupera_presos(services: Services, session: Session) -> None:
    runner = Runner(services, ["indexer", "decay"], worker_id="teste")
    stuck = queue.enqueue(session, indexer.KIND, {"document_id": LISBOA})
    session.commit()
    queue.claim(session, [indexer.KIND], "morto")
    session.execute(update(Job).values(locked_at=datetime.now(UTC) - timedelta(hours=1)))
    session.commit()

    runner.housekeeping()
    session.expire_all()
    assert session.get_one(Job, stuck).status == "pending"

    thread = threading.Thread(target=runner.run_forever)
    thread.start()
    runner.stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_escolha_dos_workers() -> None:
    assert parse_workers(None, True) == [
        "books",
        "catalog",
        "compute",
        "courses",
        "curator",
        "decay",
        "importer",
        "indexer",
        "linker",
        "mempalace",
        "neighbors",
        "release",
        "rooms",
    ]
    assert parse_workers("indexer", False) == ["indexer"]
    assert parse_workers("indexer, decay", False) == ["indexer", "decay"]
    with pytest.raises(UnknownWorker):
        parse_workers("nao-existe", False)
    with pytest.raises(UnknownWorker):
        parse_workers(None, False)


# --- o índice vetorial ----------------------------------------------------------------------------


def random_vector(rng: random.Random, similarity: float) -> list[float]:
    """Um vetor de direção aleatória com a semelhança cosseno dada à pergunta (o eixo 0)."""
    rest = [rng.gauss(0, 1) for _ in range(EMBEDDING_DIM - 1)]
    norm = math.sqrt(sum(x * x for x in rest))
    return [similarity, *(x / norm * math.sqrt(1 - similarity**2) for x in rest)]


def test_pesquisa_ignora_vetores_de_outros_modelos_mesmo_com_hnsw(
    services: Services, session: Session
) -> None:
    """Com o HNSW como único índice possível, 200 vetores de outro modelo mais próximos não
    podem tapar os 30 do modelo atual: o varrimento iterativo (pgvector 0.8) continua até
    haver resultados. (Sem ele, este teste devolve 0.)"""
    rng = random.Random(1)
    make_document(services, session, "Lisboa")
    services.store.put_chunks(session, LISBOA, [f"t{n}" for n in range(230)])
    pending = services.store.missing_vectors(session, LISBOA, "m-novo")
    other = {p.chunk_id: random_vector(rng, rng.uniform(0.7, 0.99)) for p in pending[:200]}
    wanted = {p.chunk_id: random_vector(rng, rng.uniform(0.3, 0.6)) for p in pending[200:]}
    services.store.put_vectors(session, "m-velho", other)
    services.store.put_vectors(session, "m-novo", wanted)
    session.commit()
    # Sem a chave primária o planner só tem o HNSW para servir a pesquisa.
    session.execute(text("ALTER TABLE chunk_vector DROP CONSTRAINT pk_chunk_vector"))
    session.execute(text("SET enable_seqscan = off"))

    hits = services.store.search(
        session, QUERY_VECTOR, model_id="m-novo", k=10, source_ids=[SOURCE]
    )

    assert len(hits) == 10
    assert {h.chunk_id for h in hits} <= set(wanted)
    sims = [h.similarity for h in hits]
    assert sims == sorted(sims, reverse=True)
    assert (
        services.store.search(session, QUERY_VECTOR, model_id="m-novo", k=10, source_ids=[]) == []
    )
    assert (
        services.store.search(
            session, QUERY_VECTOR, model_id="m-novo", k=10, source_ids=["zim:outra"]
        )
        == []
    )
