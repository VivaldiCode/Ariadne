"""Fase 4: o worker `neighbors`, o orçamento diário com vizinhos, o arranque a frio e /api/jobs."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ariadne import heat, queue
from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.cli import app as cli
from ariadne.config import Settings, get_settings
from ariadne.db.models import ChunkVector, DocumentVector, Job, Link, Room
from ariadne.db.models import Document as DocumentRow
from ariadne.documents import DocumentInfo, read_document, upsert_documents
from ariadne.embedder import FakeEmbedder
from ariadne.services import Services
from ariadne.vectors import iter_article_vectors, passage_text, store_vectors
from ariadne.workers import indexer
from ariadne.workers.neighbors import run_neighbors
from ariadne.workers.runner import Runner
from tests.fakes import FakeKiwix, default_fake
from tests.roomfactory import cluster_vector
from tests.test_level3 import SOURCE, make_services, unit_vector
from tests.zimfactory import make_zim

pytestmark = pytest.mark.db

MODEL = "fake/embedder@0000000"
HUB = f"{SOURCE}/Hub"
LEAVES = [f"A{n}" for n in range(1, 7)]
OTHER = f"{SOURCE}/Isolado"


def _page(title: str, links: list[str]) -> tuple[str, str]:
    anchors = " ".join(f'<a href="./{t}">{t}</a>' for t in links)
    return (title, f"<h1>{title}</h1><p>{title} fala de coisas e liga a {anchors}.</p>")


ARTICLES = {
    "Hub": _page("Hub", [*LEAVES, "Fantasma"]),  # Fantasma não existe no ZIM
    "A1": _page("Artigo um", ["Fundo"]),  # a segunda ligação (A1 → Fundo) não pode ser seguida
    **{leaf: _page(f"Artigo {leaf}", []) for leaf in LEAVES[1:]},
    "Fundo": _page("Fundo", []),
    "Isolado": _page("Isolado", []),
}


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "hub"
    directory.mkdir()
    make_zim(directory / "wikipedia_pt_teste_2026-09.zim", articles=ARTICLES, redirects={})
    return directory


@pytest.fixture
def fake() -> FakeKiwix:
    return default_fake()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def services(
    db: sessionmaker[Session], hub_dir: Path, fake: FakeKiwix, embedder: FakeEmbedder
) -> Iterator[Services]:
    services = make_services(db, hub_dir, fake, embedder)
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


def use(services: Services, session: Session, principal_id: int, document_id: str, n: int) -> None:
    """`n` leituras do agente: cada uma soma 1,0 ao calor do documento (evento de uso real)."""
    for _ in range(n):
        read_document(services, session, principal_id, document_id)


def heat_now(services: Services, session: Session, document_id: str) -> float:
    value = session.scalar(
        select(heat.document_heat(services.settings.heat_half_life_days)).where(
            DocumentRow.id == document_id
        )
    )
    assert value is not None
    return float(value)


def pending(session: Session) -> dict[str, float]:
    """document_id -> prioridade dos `index_document` pendentes."""
    session.expire_all()
    return {
        j.payload["document_id"]: j.priority
        for j in session.scalars(select(Job).where(Job.status == "pending"))
        if j.kind == indexer.KIND
    }


def edges(session: Session, kind: str = "zim_internal") -> dict[tuple[str, str], Link]:
    session.expire_all()
    return {
        (r.src_document, r.dst_document): r for r in session.scalars(select(Link)) if r.kind == kind
    }


def make_doc(services: Services, session: Session, name: str, **values: object) -> str:
    source = services.registry.get(SOURCE)
    assert source is not None
    upsert_documents(session, [DocumentInfo(source, name, name)])
    document_id = f"{SOURCE}/{name}"
    if values:
        session.execute(update(DocumentRow).where(DocumentRow.id == document_id).values(**values))
    session.commit()
    return document_id


# --- disparo e links internos ---------------------------------------------------------------


def test_documento_usado_varias_vezes_enfileira_os_vizinhos_certos(
    services: Services, session: Session, principal_id: int
) -> None:
    use(services, session, principal_id, HUB, 6)
    assert pending(session) == {}  # o uso sozinho não enfileira vizinhos
    quente = heat_now(services, session, HUB)
    assert quente == pytest.approx(6.0, abs=0.01)

    # Um dos vizinhos falhou de vez há pouco: não se insiste, mas a aresta regista-se.
    queue.enqueue(
        session,
        indexer.KIND,
        {"document_id": f"{SOURCE}/A2"},
        dedupe_key=indexer.dedupe_key(f"{SOURCE}/A2"),
    )
    session.execute(update(Job).values(status="failed", finished_at=datetime.now(UTC)))
    session.commit()

    (expansion,) = run_neighbors(services)

    assert expansion.document_id == HUB
    want = {HUB: quente * 1.0, **{f"{SOURCE}/{leaf}": quente * 0.6 for leaf in LEAVES}}
    del want[f"{SOURCE}/A2"]
    got = pending(session)
    assert got.keys() == want.keys()  # sem o Fantasma, sem o Fundo (segundo salto)
    for document_id, priority in want.items():
        assert got[document_id] == pytest.approx(priority, rel=1e-3)

    found = edges(session)
    assert set(found) == {(HUB, f"{SOURCE}/{leaf}") for leaf in LEAVES}
    edge = found[(HUB, f"{SOURCE}/A1")]
    assert (edge.score, edge.status, edge.created_by) == (1.0, "accepted", "neighbors")
    assert session.get_one(DocumentRow, f"{SOURCE}/A1").title == "Artigo um"  # título lido do ZIM
    assert session.get_one(DocumentRow, HUB).expanded_at is not None

    # Já expandido: a ronda seguinte não repete nada.
    assert run_neighbors(services) == []
    assert len(edges(session)) == len(LEAVES)


def test_documento_pouco_quente_nao_expande(
    services: Services, session: Session, principal_id: int
) -> None:
    use(services, session, principal_id, HUB, 3)
    assert run_neighbors(services) == []
    assert pending(session) == {}
    assert session.get_one(DocumentRow, HUB).expanded_at is None


def test_so_o_uso_aquece_os_vizinhos_um_salto_so(
    services: Services, session: Session, principal_id: int
) -> None:
    use(services, session, principal_id, HUB, 6)
    run_neighbors(services)
    # A1 liga a Fundo, mas A1 nunca foi usado: fica sem calor e sem expansão.
    assert session.get_one(DocumentRow, f"{SOURCE}/A1").heat == 0
    assert run_neighbors(services) == []
    assert f"{SOURCE}/Fundo" not in pending(session)
    # Quando o agente usa mesmo A1, aí sim expande-se.
    use(services, session, principal_id, f"{SOURCE}/A1", 6)
    (expansion,) = run_neighbors(services)
    assert expansion.document_id == f"{SOURCE}/A1"
    assert f"{SOURCE}/Fundo" in pending(session)


def test_limite_de_vizinhos_por_expansao(
    db: sessionmaker[Session],
    hub_dir: Path,
    fake: FakeKiwix,
    embedder: FakeEmbedder,
    session: Session,
    principal_id: int,
) -> None:
    services = make_services(db, hub_dir, fake, embedder, neighbors_max_per_doc=3)
    try:
        use(services, session, principal_id, HUB, 6)
        run_neighbors(services)
        got = pending(session)
        assert set(got) == {
            HUB,
            *(f"{SOURCE}/{leaf}" for leaf in LEAVES[:3]),
        }  # pela ordem do artigo
        assert len(edges(session)) == 3
    finally:
        services.close()


def test_volta_a_expandir_passada_uma_meia_vida_se_continua_quente(
    services: Services, session: Session, principal_id: int
) -> None:
    use(services, session, principal_id, HUB, 6)
    run_neighbors(services)
    long_ago = datetime.now(UTC) - timedelta(days=31)
    session.execute(update(DocumentRow).values(expanded_at=long_ago))
    session.commit()
    # Muito tempo depois o calor decaiu a quase nada: já não se expande.
    session.execute(update(DocumentRow).values(heat_at=long_ago))
    session.commit()
    assert run_neighbors(services) == []
    # Mas se continua a ser usado (calor outra vez alto), a vizinhança refaz-se sem duplicar.
    use(services, session, principal_id, HUB, 6)
    (again,) = run_neighbors(services)
    assert again.document_id == HUB
    assert len(edges(session)) == len(LEAVES)
    assert again.links == []  # as arestas já existiam
    session.expire_all()
    expanded_at = session.get_one(DocumentRow, HUB).expanded_at
    assert expanded_at is not None and expanded_at > long_ago


# --- reclamação, falhas e paragem ---------------------------------------------------------------


def test_o_lock_do_documento_solta_se_antes_de_ler_o_zim(
    services: Services,
    session: Session,
    principal_id: int,
    db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ariadne.workers import neighbors

    use(services, session, principal_id, HUB, 6)
    original = neighbors._internal_links
    seen: list[object] = []

    def spy(source: object, path: str) -> dict[str, str]:
        with db() as other:  # outra ligação: `search` a aquecer o documento, o indexer, …
            other.execute(text("SET LOCAL lock_timeout = '1s'"))
            row = other.execute(
                text("SELECT expanded_at FROM document WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": HUB},
            ).one()
            seen.append(row[0])
            other.rollback()
        return original(source, path)  # type: ignore[arg-type]

    monkeypatch.setattr(neighbors, "_internal_links", spy)
    run_neighbors(services)
    assert len(seen) == 1 and seen[0] is not None  # sem lock, e a marca já estava visível


def test_expansao_falhada_repoe_o_estado_anterior_e_volta_a_tentar_se(
    services: Services,
    session: Session,
    principal_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(services, session, principal_id, HUB, 6)
    old = datetime.now(UTC) - timedelta(days=40)
    session.execute(update(DocumentRow).where(DocumentRow.id == HUB).values(expanded_at=old))
    session.commit()
    real_get = services.registry.get
    monkeypatch.setattr(services.registry, "get", lambda _id: None)  # a fonte sumiu do registo

    assert run_neighbors(services) == []

    session.expire_all()
    assert session.get_one(DocumentRow, HUB).expanded_at == old  # não ficou marcado
    assert pending(session) == {}
    monkeypatch.setattr(services.registry, "get", real_get)
    assert [e.document_id for e in run_neighbors(services)] == [HUB]
    session.expire_all()
    assert session.get_one(DocumentRow, HUB).expanded_at != old


def test_cada_ronda_sincroniza_as_fontes_e_para_com_o_sinal(
    services: Services, session: Session, principal_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    use(services, session, principal_id, HUB, 6)
    calls: list[int] = []
    original = services.registry.sync_if_changed
    monkeypatch.setattr(
        services.registry, "sync_if_changed", lambda s: calls.append(1) or original(s)
    )
    stop = threading.Event()
    stop.set()
    assert run_neighbors(services, stop) == []  # parar: nenhuma expansão
    assert calls == [1]  # mas a ronda sincronizou as fontes ao começar
    assert session.get_one(DocumentRow, HUB).expanded_at is None
    assert len(run_neighbors(services)) == 1


def test_o_zim_so_se_le_uma_vez_por_expansao(
    services: Services, session: Session, principal_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ariadne.workers import neighbors

    use(services, session, principal_id, HUB, 6)
    reads: list[str] = []
    original = neighbors.read_article
    monkeypatch.setattr(
        neighbors, "read_article", lambda src, path: reads.append(path) or original(src, path)
    )
    run_neighbors(services)
    assert reads == ["Hub"]  # os títulos dos destinos vêm dos links do artigo
    assert session.get_one(DocumentRow, f"{SOURCE}/A1").title == "Artigo um"


def test_ligacao_cruzada_que_aponta_para_o_documento_quente_tambem_conta(
    services: Services, session: Session, principal_id: int
) -> None:
    make_doc(services, session, "Hub")
    isolado = make_doc(services, session, "Fundo")  # ordena antes do Hub: a aresta aponta para ele
    session.add(Link(src_document=isolado, dst_document=HUB, kind="title_match", status="accepted"))
    session.commit()
    use(services, session, principal_id, HUB, 6)
    run_neighbors(services)
    assert pending(session)[isolado] == pytest.approx(
        heat_now(services, session, HUB) * 0.8, rel=1e-3
    )


def test_pendentes_vem_primeiro_os_devidos_e_depois_os_adiados(session: Session) -> None:
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    queue.enqueue(session, "k", {"n": "adiado-alto"}, priority=9.0, run_after=tomorrow)
    queue.enqueue(session, "k", {"n": "devido-baixo"}, priority=1.0)
    queue.enqueue(session, "k", {"n": "devido-alto"}, priority=2.0)
    session.commit()
    assert [j.payload["n"] for j in queue.upcoming(session, 10)] == [
        "devido-alto",
        "devido-baixo",
        "adiado-alto",
    ]


# --- ligações cruzadas, sala e semântico -------------------------------------------------------


def test_ligacoes_cruzadas_aceites_entram_com_peso_08(
    services: Services, session: Session, principal_id: int
) -> None:
    make_doc(services, session, "Hub")
    isolado = make_doc(services, session, "Isolado")
    rejeitado = make_doc(services, session, "Fundo")
    session.add_all(
        [
            Link(src_document=HUB, dst_document=isolado, kind="title_match", status="accepted"),
            Link(src_document=rejeitado, dst_document=HUB, kind="title_match", status="rejected"),
        ]
    )
    session.commit()
    use(services, session, principal_id, HUB, 6)
    run_neighbors(services)
    quente = heat_now(services, session, HUB)
    got = pending(session)
    assert got[isolado] == pytest.approx(quente * 0.8, rel=1e-3)
    assert rejeitado not in got


def make_room(session: Session, label: str) -> int:
    room = Room(wing="wikipedia_pt_teste", label=label, model_id=MODEL, centroid=unit_vector(1.0))
    session.add(room)
    session.commit()
    return room.id


def test_sala_limita_aos_mais_quentes_e_sem_sala_nao_faz_nada(
    services: Services, session: Session, principal_id: int
) -> None:
    sala, outra = make_room(session, "sala"), make_room(session, "outra")
    for name, heat_value in (("Isolado", 4.0), ("Fundo", 2.0), ("A6", 1.0)):
        make_doc(services, session, name, room=sala, heat=heat_value)
    make_doc(services, session, "A5", room=outra, heat=3.0)
    use(services, session, principal_id, HUB, 6)
    session.execute(update(DocumentRow).where(DocumentRow.id == HUB).values(room=sala))
    session.commit()
    services.settings.neighbors_room_limit = 2

    run_neighbors(services)

    quente = heat_now(services, session, HUB)
    got = pending(session)
    # Isolado e Fundo são os 2 mais quentes da sala; A6 é do HUB por link (0,6), não pela sala.
    assert got[f"{SOURCE}/Isolado"] == pytest.approx(quente * 0.4, rel=1e-3)
    assert got[f"{SOURCE}/Fundo"] == pytest.approx(quente * 0.4, rel=1e-3)
    assert got[f"{SOURCE}/A6"] == pytest.approx(quente * 0.6, rel=1e-3)  # o maior dos pesos
    assert got[f"{SOURCE}/A5"] == pytest.approx(quente * 0.6, rel=1e-3)  # só por link, outra sala


def test_vizinhos_de_sala_com_as_salas_calculadas_dos_vetores_de_artigo(
    services: Services, session: Session, principal_id: int
) -> None:
    """Os vetores de artigo → `run_rooms` → a sala do Hub traz os colegas dela, não os de fora."""
    from ariadne.rooms_build import run_rooms

    groups = {0: ["Hub", "Isolado", "Fundo", "A6"], 1: ["A2", "A3", "A4", "A5"]}
    for axis, names in groups.items():
        for position, name in enumerate(names):
            document_id = make_doc(services, session, name)
            session.add(
                DocumentVector(
                    document_id=document_id,
                    model_id=MODEL,
                    embedding=cluster_vector(axis, 0.01 * position, side=100 + position),
                )
            )
    session.commit()
    services.settings.rooms_min = services.settings.rooms_max = 2
    services.settings.rooms_min_documents = 6
    run_rooms(services)
    assert session.get_one(DocumentRow, HUB).room == session.get_one(DocumentRow, OTHER).room
    use(services, session, principal_id, HUB, 6)
    services.settings.neighbors_semantic_k = 0  # só a sala (e os links do ZIM) entram

    run_neighbors(services)

    quente = heat_now(services, session, HUB)
    got = pending(session)
    # Isolado e Fundo não têm link a partir do Hub: vêm só pela sala (0,4). A2 é de outra sala e
    # vem pelo link do Hub (0,6); A6 é das duas coisas e fica com o maior peso.
    assert got[OTHER] == pytest.approx(quente * 0.4, rel=1e-3)
    assert got[f"{SOURCE}/Fundo"] == pytest.approx(quente * 0.4, rel=1e-3)
    assert got[f"{SOURCE}/A2"] == pytest.approx(quente * 0.6, rel=1e-3)
    assert got[f"{SOURCE}/A6"] == pytest.approx(quente * 0.6, rel=1e-3)


def test_semantico_usa_os_k_mais_proximos_do_modelo_atual(
    services: Services, session: Session, principal_id: int
) -> None:
    make_doc(services, session, "Hub")
    near, middle, far = (make_doc(services, session, n) for n in ("Isolado", "Fundo", "A6"))
    other = make_doc(services, session, "A5")
    session.add_all(
        [
            DocumentVector(document_id=HUB, model_id=MODEL, embedding=unit_vector(1.0)),
            DocumentVector(document_id=near, model_id=MODEL, embedding=unit_vector(0.9)),
            DocumentVector(document_id=middle, model_id=MODEL, embedding=unit_vector(0.6)),
            DocumentVector(document_id=far, model_id=MODEL, embedding=unit_vector(0.1)),
            DocumentVector(document_id=other, model_id="outro", embedding=unit_vector(0.99)),
        ]
    )
    session.commit()
    use(services, session, principal_id, HUB, 6)
    services.settings.neighbors_semantic_k = 2

    run_neighbors(services)

    quente = heat_now(services, session, HUB)
    got = pending(session)
    assert got[near] == pytest.approx(quente * 0.5, rel=1e-3)
    assert got[middle] == pytest.approx(quente * 0.5, rel=1e-3)
    # `far` só entra pelo link do ZIM (0,6), não pelo semântico (ficou fora do k=2)
    assert got[far] == pytest.approx(quente * 0.6, rel=1e-3)
    assert (
        services.store.similar_documents(session, other, MODEL, 5) == []
    )  # sem vetor deste modelo


def test_semantico_funciona_com_os_vetores_de_artigo_gerados_pelo_pipeline(
    services: Services, session: Session, principal_id: int, embedder: FakeEmbedder
) -> None:
    """`vectors build` -> `document_vector` (com índice HNSW) -> o worker enfileira o vizinho."""
    source = services.registry.get(SOURCE)
    assert source is not None
    for path, vector in (
        ("Hub", unit_vector(1.0)),
        ("Isolado", unit_vector(0.9, 2)),
        ("Fundo", unit_vector(0.2, 3)),
    ):
        embedder.vectors[passage_text(source.read(path))] = vector
    for rows in iter_article_vectors(source, embedder, batch=3):
        store_vectors(session, services.store, rows)
    session.commit()
    use(services, session, principal_id, HUB, 6)
    services.settings.neighbors_semantic_k = 1

    run_neighbors(services)

    quente = heat_now(services, session, HUB)
    got = pending(session)
    # Isolado não está ligado ao Hub pelo ZIM: só o vizinho semântico (0,5) o traz.
    assert got[OTHER] == pytest.approx(quente * 0.5, rel=1e-3)


def test_sem_embedder_a_ronda_espera_e_nada_fica_marcado(
    services: Services, session: Session, principal_id: int
) -> None:
    use(services, session, principal_id, HUB, 6)
    services.embedder = None
    services.settings.embedder = "http"
    services.settings.embedder_url = ""  # configuração em falta: EmbedderError
    assert run_neighbors(services) == []
    assert session.get_one(DocumentRow, HUB).expanded_at is None


# --- orçamento diário ------------------------------------------------------------------------


def test_orcamento_pequeno_indexa_so_os_de_maior_prioridade_e_o_resto_fica_para_amanha(
    db: sessionmaker[Session],
    hub_dir: Path,
    fake: FakeKiwix,
    embedder: FakeEmbedder,
    session: Session,
    principal_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O dia do orçamento é o dia UTC: com o relógio do indexador congelado, o teste não muda de dia
    # (nem renova o orçamento) a meio quando corre à volta da meia-noite UTC.
    clock = {"now": datetime.now(UTC)}

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> "FrozenDatetime":  # type: ignore[override]
            return cls.fromtimestamp(clock["now"].timestamp(), UTC)

    monkeypatch.setattr(indexer, "datetime", FrozenDatetime)
    services = make_services(db, hub_dir, fake, embedder, index_daily_budget=3)
    try:
        make_doc(services, session, "Hub")
        isolado = make_doc(services, session, "Isolado")
        session.add(Link(src_document=HUB, dst_document=isolado, kind="title_match"))
        session.commit()
        use(services, session, principal_id, HUB, 6)
        run_neighbors(services)
        total = len(pending(session))  # Hub (1,0), Isolado (0,8) e as 6 folhas (0,6)
        assert total == 8

        runner = Runner(services, ["indexer"], worker_id="teste")
        while runner.run_once():
            pass

        indexed = services.store.indexed(
            session, [HUB, isolado, *(f"{SOURCE}/{leaf}" for leaf in LEAVES)], MODEL
        )
        # Os três de maior prioridade: Hub (1,0), Isolado (0,8) e o primeiro dos de 0,6.
        assert len(indexed) == 3 and {HUB, isolado} < indexed
        left = pending(session)
        assert len(left) == total - 3 and not set(left) & indexed
        midnight = indexer.start_of_day(clock["now"]) + timedelta(days=1)
        session.expire_all()
        for job in session.scalars(select(Job).where(Job.status == "pending")):
            assert (job.run_after, job.attempts) == (midnight, 0)  # adiado, nada se perdeu
        assert (
            session.scalar(select(func.count()).select_from(Job).where(Job.status == "failed")) == 0
        )

        # Chega o dia seguinte: o orçamento renova-se e continua pela mesma ordem.
        clock["now"] += timedelta(days=1)
        for _ in range(total):
            session.execute(
                update(ChunkVector).values(created_at=datetime.now(UTC) - timedelta(days=1))
            )
            session.execute(
                update(Job).where(Job.status == "pending").values(run_after=datetime.now(UTC))
            )
            session.commit()
            while runner.run_once():
                pass
            if not pending(session):
                break
        assert pending(session) == {}
        assert (
            len(
                services.store.indexed(
                    session, [HUB, isolado, *(f"{SOURCE}/{leaf}" for leaf in LEAVES)], MODEL
                )
            )
            == total
        )
    finally:
        services.close()


def test_runner_corre_o_neighbors_como_tarefa_periodica(
    services: Services, session: Session, principal_id: int
) -> None:
    use(services, session, principal_id, HUB, 6)
    runner = Runner(services, ["neighbors"], worker_id="teste")
    runner.housekeeping()
    assert HUB in pending(session)
    assert not runner.run_once()  # o neighbors não tem fila própria


# --- /api/jobs -----------------------------------------------------------------------------------


@pytest.fixture
def api(database_url: str, db: sessionmaker[Session], hub_dir: Path) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, key = create_principal(session, PrincipalKind.user, "pessoa", Role.admin)
    settings = Settings(
        database_url=database_url,
        zim_dir=hub_dir,
        kiwix_url="http://kiwix.invalido",
        index_daily_budget=10,
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {key}"}) as client:
        yield client


def test_api_jobs_pede_chave(api: TestClient) -> None:
    api.headers.pop("Authorization")
    assert api.get("/api/jobs").status_code == 401


def test_api_jobs_mostra_contagens_orcamento_e_proximos(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    services: Services = api.app.state.services  # type: ignore[attr-defined]
    with db() as session:
        make_doc(services, session, "Isolado")
        for name, priority in (("Isolado", 2.0), ("Fundo", 5.0), ("A1", 0.5)):
            indexer.enqueue_index(session, f"{SOURCE}/{name}", priority)
        queue.enqueue(session, "outro_tipo", {"x": 1})
        session.commit()
        source = services.registry.get(SOURCE)
        assert source is not None
        chunk_ids = []
        services.store.put_chunks(session, f"{SOURCE}/Isolado", ["um", "dois", "tres"])
        for pend in services.store.missing_vectors(session, f"{SOURCE}/Isolado", MODEL):
            chunk_ids.append(pend.chunk_id)
        services.store.put_vectors(session, MODEL, {c: unit_vector(0.5) for c in chunk_ids})
        session.commit()

    body = api.get("/api/jobs", params={"limit": 2}).json()

    assert body["counts"] == {"index_document": {"pending": 3}, "outro_tipo": {"pending": 1}}
    assert body["budget"]["daily"] == 10
    assert (body["budget"]["used"], body["budget"]["remaining"]) == (3, 7)
    assert body["budget"]["model_id"] == MODEL
    assert datetime.fromisoformat(body["budget"]["resets_at"]) > datetime.now(UTC)
    assert [(j["document_id"], j["priority"], j["due"]) for j in body["next"]] == [
        (f"{SOURCE}/Fundo", 5.0, True),
        (f"{SOURCE}/Isolado", 2.0, True),
    ]
    assert api.get("/api/jobs", params={"limit": 0}).status_code == 422


# --- arranque a frio: ariadne index enqueue -------------------------------------------------------


@pytest.fixture
def cold_env(monkeypatch: pytest.MonkeyPatch, database_url: str, hub_dir: Path) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ZIM_DIR", str(hub_dir))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_index_enqueue_semeia_a_fila_com_a_prioridade_pedida(
    cold_env: None, session: Session, tmp_path: Path
) -> None:
    lista = tmp_path / "semente.txt"
    lista.write_text(
        f"# colecao de arranque\nHub\n{SOURCE}/Fundo\n\nHub\nNaoExiste\nIsolado\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli, ["index", "enqueue", "--source", SOURCE, "--paths", str(lista), "--priority", "0.25"]
    )

    assert result.exit_code == 0, result.output
    assert pending(session) == {HUB: 0.25, f"{SOURCE}/Fundo": 0.25, OTHER: 0.25}
    assert "3 documents" in result.output and "1 ignored" in result.output

    # Repetir não duplica; --limit sem lista usa os primeiros da fonte.
    again = CliRunner().invoke(cli, ["index", "enqueue", "--source", SOURCE, "--limit", "2"])
    assert again.exit_code == 0, again.output
    assert len(pending(session)) in (3, 4, 5)
    assert session.scalar(select(func.count()).select_from(Job)) == len(pending(session))


def test_cli_index_enqueue_pede_lista_ou_limite_e_fonte_valida(cold_env: None) -> None:
    assert CliRunner().invoke(cli, ["index", "enqueue", "--source", SOURCE]).exit_code == 2
    unknown = CliRunner().invoke(cli, ["index", "enqueue", "--source", "zim:x", "--limit", "1"])
    assert unknown.exit_code == 1


def test_cli_index_enqueue_valida_a_prioridade(cold_env: None) -> None:
    for bad in ("-1", "nan", "inf"):
        result = CliRunner().invoke(
            cli, ["index", "enqueue", "--source", SOURCE, "--limit", "1", "--priority", bad]
        )
        assert result.exit_code == 2, bad


def test_cli_index_enqueue_da_fonte_nao_le_artigos_e_salta_indexados_e_falhados(
    cold_env: None,
    services: Services,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ariadne.cli

    def must_not_read(*_: object) -> None:
        raise AssertionError("o iter_paths já dá caminhos canónicos: não se relê o artigo")

    monkeypatch.setattr(ariadne.cli, "read_article", must_not_read)
    fundo = make_doc(services, session, "Fundo")
    services.store.mark_indexed(session, fundo, MODEL, services.registry.get(SOURCE).version)  # type: ignore[union-attr]
    queue.enqueue(
        session, indexer.KIND, {"document_id": OTHER}, dedupe_key=indexer.dedupe_key(OTHER)
    )
    session.execute(update(Job).values(status="failed", finished_at=datetime.now(UTC)))
    session.commit()

    result = CliRunner().invoke(cli, ["index", "enqueue", "--source", SOURCE, "--limit", "100"])

    assert result.exit_code == 0, result.output
    assert set(pending(session)) == {f"{SOURCE}/{n}" for n in ARTICLES} - {fundo, OTHER}
    assert "2 already indexed or recently failed" in result.output
