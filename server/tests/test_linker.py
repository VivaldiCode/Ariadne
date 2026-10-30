"""Fase 5: o `linker` (title_match e vetores) entre dois ZIMs minúsculos."""

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, create_principal
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import DocumentVector, Link, Source
from ariadne.documents import DocumentInfo, add_aliases, read_document, upsert_documents
from ariadne.embedder import FakeEmbedder
from ariadne.services import Services
from ariadne.workers import linker
from ariadne.workers.linker import LinkReport, run_linker
from ariadne.workers.runner import Runner
from tests.fakes import default_fake
from tests.graphzims import REVOLUTION, WIKIBOOKS, WIKIPEDIA, make_graph_dir
from tests.test_level3 import make_services, unit_vector

pytestmark = pytest.mark.db

MODEL = "fake/embedder@0000000"
PEDIA_REV = f"{WIKIPEDIA}/{REVOLUTION}"
BOOKS_REV = f"{WIKIBOOKS}/{REVOLUTION}"
BOOKS_ABRIL = f"{WIKIBOOKS}/25_de_Abril"


@pytest.fixture
def zims(tmp_path: Path) -> Path:
    return make_graph_dir(tmp_path / "graph")


@pytest.fixture
def services(db: sessionmaker[Session], zims: Path) -> Iterator[Services]:
    services = make_services(db, zims, default_fake(), FakeEmbedder())
    yield services
    services.close()


@pytest.fixture
def session(db: sessionmaker[Session]) -> Iterator[Session]:
    with db() as s:
        yield s


def add_doc(
    services: Services,
    session: Session,
    document_id: str,
    title: str | None = None,
    vector: list[float] | None = None,
    aliases: tuple[str, ...] = (),
) -> None:
    source_id, path = document_id.split("/", 1)
    source = services.registry.get(source_id)
    assert source is not None
    upsert_documents(session, [DocumentInfo(source, path, title or path, aliases)])
    if vector is not None:
        services.store.put_article_vectors(session, MODEL, {document_id: vector})
    session.commit()


def links(session: Session, kind: str | None = None) -> dict[tuple[str, str], Link]:
    session.expire_all()
    query = select(Link).order_by(Link.id)
    if kind is not None:
        query = query.where(Link.kind == kind)
    return {(link.src_document, link.dst_document): link for link in session.scalars(query)}


# --- title_match ------------------------------------------------------------------------------


def test_titulo_igual_entre_fontes_cria_aresta_aceite(services: Services, session: Session) -> None:
    for source_id, path in ((WIKIPEDIA, REVOLUTION), (WIKIBOOKS, REVOLUTION)):
        add_doc(services, session, f"{source_id}/{path}", "Revolução dos Cravos")
    add_doc(services, session, f"{WIKIPEDIA}/Lisboa", "Lisboa")
    add_doc(services, session, f"{WIKIBOOKS}/Receitas", "Receitas")

    report = run_linker(services)

    assert (report.documents, report.title_links) == (4, 1)
    [(pair, link)] = links(session).items()
    assert pair == (BOOKS_REV, PEDIA_REV)  # não dirigida: guardada com src < dst
    assert (link.kind, link.score, link.status, link.created_by) == (
        "title_match",
        1.0,
        "accepted",
        "linker",
    )
    assert link.model_id is None
    again = run_linker(services)  # nada por ver, nada de novo
    assert (again.documents, again.title_links) == (0, 0)


def test_normalizacao_ignora_maiusculas_underscores_e_pontuacao_final(
    services: Services, session: Session
) -> None:
    add_doc(services, session, f"{WIKIPEDIA}/Lisboa", "Lisboa_do  Norte")
    add_doc(services, session, f"{WIKIBOOKS}/Lisboa", "LISBOA do norte.")

    assert run_linker(services).title_links == 1


def test_titulo_alternativo_do_redirect_liga_a_outra_fonte(
    services: Services, session: Session, db: sessionmaker[Session]
) -> None:
    with db() as reading:
        principal_id = create_principal(reading, PrincipalKind.agent, "leitor")[0].id
        reading.commit()
        # Lê-se pelo redirect «25 de Abril»: o documento fica com esse título como alias.
        read_document(services, reading, principal_id, f"{WIKIPEDIA}/25_de_Abril")
    add_doc(services, session, BOOKS_ABRIL, "25 de abril")  # título igual ao alias
    add_doc(services, session, f"{WIKIBOOKS}/Receitas", "Receitas")

    run_linker(services)

    # por alias, sem vetores que o confirmem: fica proposta, sem pontuação
    [link] = links(session, "title_match").values()
    assert (link.src_document, link.dst_document) == (BOOKS_ABRIL, PEDIA_REV)
    assert (link.status, link.score) == ("proposed", None)
    meta = session.scalar(select(DocumentRow.meta).where(DocumentRow.id == PEDIA_REV))
    assert meta == {"aliases": ["25 de Abril"]}


def test_documento_que_chega_depois_encontra_o_que_ja_estava(
    services: Services, session: Session
) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    first = run_linker(services)
    assert (first.documents, first.title_links) == (1, 0)

    add_doc(services, session, BOOKS_REV, "Revolução dos Cravos")
    second = run_linker(services)

    assert (second.documents, second.title_links) == (1, 1)  # só o novo foi visto
    assert list(links(session)) == [(BOOKS_REV, PEDIA_REV)]


def test_alias_novo_faz_o_linker_rever_o_documento(services: Services, session: Session) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    add_doc(services, session, BOOKS_ABRIL, "25 de abril")
    assert run_linker(services).title_links == 0

    assert add_aliases(session, {PEDIA_REV: ["25 de Abril", "revolução dos cravos"]}) == 1
    session.commit()
    again = run_linker(services)

    assert (again.documents, again.title_links) == (1, 1)
    assert list(links(session)) == [(BOOKS_ABRIL, PEDIA_REV)]
    meta = session.scalar(select(DocumentRow.meta).where(DocumentRow.id == PEDIA_REV))
    assert meta == {"aliases": ["25 de Abril"]}  # o igual ao título não conta como alias
    assert add_aliases(session, {PEDIA_REV: ["25 DE ABRIL"]}) == 0  # nem os repetidos


def test_titulo_igual_na_mesma_fonte_nao_liga(services: Services, session: Session) -> None:
    add_doc(services, session, f"{WIKIPEDIA}/Lisboa", "Lisboa")
    add_doc(services, session, f"{WIKIPEDIA}/Lisboa_(cidade)", "Lisboa")

    assert run_linker(services).title_links == 0
    assert links(session) == {}


def test_aresta_rejeitada_nao_ressuscita(services: Services, session: Session) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    add_doc(services, session, BOOKS_REV, "Revolução dos Cravos")
    run_linker(services)
    session.execute(update(Link).values(status="rejected", reviewed_by="curadora"))
    session.execute(update(DocumentRow).values(linked_at=None))
    session.commit()

    report = run_linker(services)

    assert (report.documents, report.title_links) == (2, 0)
    [link] = links(session).values()
    assert (link.status, link.reviewed_by) == ("rejected", "curadora")


def test_fonte_que_sumiu_nao_liga(services: Services, session: Session) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos", vector=unit_vector(1.0))
    add_doc(services, session, BOOKS_REV, "Revolução dos Cravos", vector=unit_vector(0.99, 2))
    session.execute(update(Source).where(Source.id == WIKIBOOKS).values(status="missing"))
    session.commit()

    report = run_linker(services)

    assert (report.documents, report.title_links, report.accepted) == (2, 0, 0)
    assert links(session) == {}


# --- vector -----------------------------------------------------------------------------------

A, B, C, D, E = (
    f"{WIKIPEDIA}/Lisboa",  # o ponto de referência (eixo 0)
    f"{WIKIBOOKS}/Receitas",  # cosseno 0,96 com A: aceite
    f"{WIKIBOOKS}/25_de_Abril",  # 0,90 com A: proposta
    f"{WIKIBOOKS}/{REVOLUTION}",  # 0,80 com A: abaixo do limiar
    f"{WIKIPEDIA}/Cozinha",  # 0,999 com A, mas da mesma fonte
)


def seed_vectors(services: Services, session: Session) -> None:
    for document_id, vector in (
        (A, unit_vector(1.0)),
        (B, unit_vector(0.96, 1)),
        (C, unit_vector(0.90, 2)),
        (D, unit_vector(0.80, 3)),
        (E, unit_vector(0.999, 4)),
    ):
        add_doc(services, session, document_id, vector=vector)


def test_vetores_propoem_e_aceitam_conforme_os_limiares_e_so_entre_fontes(
    services: Services, session: Session
) -> None:
    seed_vectors(services, session)

    report = run_linker(services)

    got = links(session, "vector")
    # B e C são do Wikilivros: só se ligam às da Wikipédia (A e E). A-E é da mesma fonte.
    assert {k: (round(v.score or 0, 3), v.status) for k, v in got.items()} == {
        (B, A): (0.96, "accepted"),
        (C, A): (0.9, "proposed"),
        (B, E): (0.959, "accepted"),
        (C, E): (0.899, "proposed"),
    }
    assert (report.accepted, report.proposed) == (2, 2)
    assert {(v.model_id, v.created_by) for v in got.values()} == {(MODEL, "linker")}
    assert D not in {d for pair in got for d in pair}  # 0,80: abaixo de LINK_TAU


def test_aresta_vetorial_e_uma_so_vista_dos_dois_lados_e_idempotente(
    services: Services, session: Session
) -> None:
    seed_vectors(services, session)
    run_linker(services)
    before = {k: v.id for k, v in links(session).items()}
    session.execute(update(DocumentRow).values(linked_at=None))  # vê-se tudo de novo
    session.commit()

    report = run_linker(services)

    assert (report.accepted, report.proposed) == (0, 0)
    assert {k: v.id for k, v in links(session).items()} == before


def test_limiares_vem_da_configuracao(services: Services, session: Session) -> None:
    seed_vectors(services, session)
    services.settings.link_tau = 0.85
    services.settings.link_tau_accept = 0.92

    run_linker(services)

    status = {k: v.status for k, v in links(session, "vector").items()}
    assert status[(C, A)] == "proposed" and status[(B, A)] == "accepted"
    assert (D, A) not in status  # 0,80 < 0,85


def test_vetor_de_outro_modelo_e_ignorado_e_vetor_novo_faz_rever_o_documento(
    services: Services, session: Session
) -> None:
    add_doc(services, session, A, vector=unit_vector(1.0))
    add_doc(services, session, B)  # sem vetor ainda
    session.add(DocumentVector(document_id=B, model_id="outro@1", embedding=unit_vector(0.99, 1)))
    session.commit()
    assert run_linker(services).documents == 2
    assert links(session) == {}

    services.store.put_article_vectors(session, MODEL, {B: unit_vector(0.99, 1)})
    session.commit()
    report = run_linker(services)

    assert (report.documents, report.accepted) == (1, 1)  # só o B voltou a ser visto
    assert list(links(session, "vector")) == [(B, A)]


def test_aprofunda_a_busca_quando_os_vizinhos_proximos_sao_da_mesma_fonte(
    services: Services, session: Session
) -> None:
    """O HNSW devolve primeiro os da própria fonte; os de outras fontes só aparecem mais fundo."""
    add_doc(services, session, B, vector=unit_vector(0.97, 1))
    own = {}
    for n in range(130):  # mais de MIN_CANDIDATES, todos mais perto de A do que B
        document_id = f"{WIKIPEDIA}/Irma{n:03d}"
        add_doc(services, session, document_id)
        own[document_id] = unit_vector(0.99 + n * 1e-5, 5 + n % 100)
    services.store.put_article_vectors(session, MODEL, own)
    add_doc(services, session, A, vector=unit_vector(1.0))

    found = services.store.similar_across_sources(session, A, MODEL, 0.88, 5)

    assert [d for d, _ in found] == [B]


def seed_fan(services: Services, session: Session) -> None:
    add_doc(services, session, A, vector=unit_vector(1.0))
    for n in range(4):
        add_doc(services, session, f"{WIKIBOOKS}/L{n}", vector=unit_vector(0.99 - n * 0.01, 1 + n))


def test_cada_documento_cria_no_maximo_link_max_por_doc_ligacoes_as_mais_parecidas(
    services: Services, session: Session
) -> None:
    seed_fan(services, session)
    session.execute(update(DocumentRow).where(DocumentRow.id != A).values(linked_at=func.now()))
    session.commit()
    services.settings.link_max_per_doc = 2

    report = run_linker(services)  # só o A está por ver

    assert report.documents == 1
    assert sorted(links(session, "vector")) == [(f"{WIKIBOOKS}/L0", A), (f"{WIKIBOOKS}/L1", A)]


def test_cada_execucao_ve_no_maximo_link_batch_documentos(
    services: Services, session: Session
) -> None:
    seed_fan(services, session)
    services.settings.link_batch = 2

    seen = [run_linker(services).documents for _ in range(4)]

    assert seen == [2, 2, 1, 0]
    assert len(links(session, "vector")) == 4  # cada Lk a ligar ao A


# --- ciclo do worker --------------------------------------------------------------------------


def test_sem_embedder_a_execucao_espera_e_nada_fica_marcado(
    services: Services, session: Session
) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    services.embedder = None
    services.settings.embedder = "http"
    services.settings.embedder_url = ""

    assert run_linker(services) == LinkReport()
    assert session.get_one(DocumentRow, PEDIA_REV).linked_at is None


def test_para_entre_lotes_quando_mandado_parar(services: Services, session: Session) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    stop = threading.Event()
    stop.set()

    assert run_linker(services, stop).documents == 0
    assert session.get_one(DocumentRow, PEDIA_REV).linked_at is None


def test_runner_corre_o_linker_como_tarefa_periodica(services: Services, session: Session) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    add_doc(services, session, BOOKS_REV, "Revolução dos Cravos")
    runner = Runner(services, ["linker"], worker_id="teste")

    runner.housekeeping()

    assert list(links(session)) == [(BOOKS_REV, PEDIA_REV)]
    assert not runner.run_once()  # o linker não tem fila própria
    assert "linker" in Runner(services, ["linker"]).periodic


# --- title_match: chaves fracas, línguas e confirmação por vetor ------------------------------


def test_titulos_curtos_numericos_e_genericos_nao_ligam(
    services: Services, session: Session
) -> None:
    for title in ("ABC", "1974", "Main Page", "Índice", "Lisboa do Norte"):
        add_doc(services, session, f"{WIKIPEDIA}/{title.replace(' ', '_')}", title)
        add_doc(services, session, f"{WIKIBOOKS}/{title.replace(' ', '_')}", title)

    report = run_linker(services)

    assert report.title_links == 1
    [pair] = links(session)
    assert pair == (f"{WIKIBOOKS}/Lisboa_do_Norte", f"{WIKIPEDIA}/Lisboa_do_Norte")


def test_titulo_que_identifica_muitos_documentos_da_mesma_fonte_nao_liga(
    services: Services, session: Session
) -> None:
    for n in range(4):  # 4 > LINK_TITLE_MAX_PER_SOURCE (3): «Lisboa» deixa de identificar algo
        add_doc(services, session, f"{WIKIPEDIA}/Lisboa_{n}", "Lisboa")
    add_doc(services, session, f"{WIKIBOOKS}/Lisboa", "Lisboa")
    add_doc(services, session, f"{WIKIPEDIA}/Porto_0", "Porto")
    add_doc(services, session, f"{WIKIBOOKS}/Porto", "Porto")

    run_linker(services)

    assert list(links(session)) == [(f"{WIKIBOOKS}/Porto", f"{WIKIPEDIA}/Porto_0")]


def test_tecto_de_arestas_por_chave(services: Services, session: Session) -> None:
    for source in (WIKIPEDIA, WIKIBOOKS):
        for n in range(3):
            add_doc(services, session, f"{source}/Lisboa_{n}", "Lisboa")
    services.settings.link_max_per_key = 4

    report = run_linker(services)

    assert report.title_links == 4  # 9 pares possíveis, 4 pelo tecto
    assert len(links(session)) == 4


def test_titulo_igual_em_linguas_diferentes_so_propoe_e_o_vetor_confirma(
    services: Services, session: Session
) -> None:
    pt, en = f"{WIKIPEDIA}/Paris", f"{WIKIBOOKS}/Paris"
    add_doc(services, session, pt, "Paris")
    add_doc(services, session, en, "Paris")
    session.execute(update(DocumentRow).where(DocumentRow.id == en).values(language="en"))
    session.commit()

    run_linker(services)

    [link] = links(session, "title_match").values()
    assert (link.status, link.score) == ("proposed", None)

    # Chega o vetor dos dois, parecidos: a proposta do linker passa a aceite (sem duplicar).
    services.store.put_article_vectors(
        session, MODEL, {pt: unit_vector(1.0), en: unit_vector(0.95, 2)}
    )
    session.commit()
    report = run_linker(services)

    [upgraded] = links(session, "title_match").values()
    assert upgraded.id == link.id
    assert (upgraded.status, round(upgraded.score or 0, 2), upgraded.model_id) == (
        "accepted",
        0.95,
        MODEL,
    )
    assert report.title_links >= 1


def test_titulo_igual_com_vetores_diferentes_fica_proposto_com_o_cosseno(
    services: Services, session: Session
) -> None:
    pt, other = f"{WIKIPEDIA}/Mercúrio", f"{WIKIBOOKS}/Mercúrio"
    add_doc(services, session, pt, "Mercúrio", vector=unit_vector(1.0))
    add_doc(services, session, other, "Mercúrio", vector=unit_vector(0.3, 2))
    session.execute(update(DocumentRow).where(DocumentRow.id == other).values(language="en"))
    session.commit()

    run_linker(services)

    [link] = links(session, "title_match").values()
    assert link.status == "proposed" and round(link.score or 0, 2) == 0.3


def test_titulo_que_muda_apaga_as_title_match_antigas_do_linker(
    services: Services, session: Session
) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    add_doc(services, session, BOOKS_REV, "Revolução dos Cravos")
    run_linker(services)
    assert len(links(session)) == 1

    add_doc(services, session, BOOKS_REV, "Outro assunto qualquer")  # o título mudou
    report = run_linker(services)

    assert report.documents == 1  # o upsert repôs o documento na fila
    assert links(session) == {}


def test_title_match_revisto_por_pessoa_nao_se_apaga(services: Services, session: Session) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    add_doc(services, session, BOOKS_REV, "Revolução dos Cravos")
    run_linker(services)
    session.execute(update(Link).values(status="accepted", reviewed_by="7"))
    session.commit()

    add_doc(services, session, BOOKS_REV, "Outro assunto qualquer")
    run_linker(services)

    assert len(links(session)) == 1


# --- falhas, concessões e atraso --------------------------------------------------------------


def seed_three(services: Services, session: Session) -> tuple[str, str, str]:
    ids = (f"{WIKIBOOKS}/A", f"{WIKIBOOKS}/B", f"{WIKIBOOKS}/C")
    for document_id in ids:
        add_doc(services, session, document_id, document_id.rsplit("/", 1)[1] * 5)
    return ids


def test_documento_que_falha_sempre_e_isolado_e_nao_trava_os_outros(
    services: Services, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c = seed_three(services, session)
    services.settings.link_lease_minutes = (
        0  # a concessão caduca logo: tenta-se na execução seguinte
    )
    original = linker._title_match

    def poisoned(svc: Services, sess: Session, ids: list[str], *args: object) -> None:
        if b in ids:
            raise RuntimeError("documento envenenado")
        original(svc, sess, ids, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(linker, "_title_match", poisoned)

    first = run_linker(services)

    assert (first.documents, first.failed) == (3, 1)
    session.expire_all()
    seen = {d: session.get_one(DocumentRow, d) for d in (a, b, c)}
    assert seen[a].linked_at is not None and seen[c].linked_at is not None
    assert seen[b].linked_at is None and seen[b].link_attempts == 1  # tentativa contada
    assert seen[a].link_lease is None and seen[b].link_lease is not None

    run_linker(services)
    third = run_linker(services)  # 3.ª tentativa: o B esgota-as
    assert third.failed == 1
    session.expire_all()
    assert session.get_one(DocumentRow, b).link_attempts == 3

    fourth = run_linker(services)  # o B já não é tentado
    assert (fourth.documents, fourth.failed) == (0, 0)
    # os outros continuam a poder ser vistos se mudarem
    add_doc(services, session, a, "Novo título A")
    assert run_linker(services).documents == 1


def test_documento_reclamado_nao_e_reclamado_por_outro_ate_a_concessao_caducar(
    services: Services, session: Session
) -> None:
    seed_three(services, session)
    with services.sessions() as other:
        first, lease = linker._claim(services, other, 10)
        other.commit()
    assert len(first) == 3 and lease is not None

    assert run_linker(services).documents == 0  # concessão em curso

    services.settings.link_lease_minutes = 0
    assert run_linker(services).documents == 3  # caducou (worker morto)


def test_alteracao_a_meio_do_lote_deixa_o_documento_por_ver(
    services: Services, session: Session
) -> None:
    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    with services.sessions() as other:
        claimed, lease = linker._claim(services, other, 10)
        other.commit()
    add_aliases(session, {PEDIA_REV: ["25 de Abril"]})  # alguém acrescenta um alias entretanto
    session.commit()

    model_id = MODEL
    linker._process(services, claimed, lease, model_id, LinkReport())

    session.expire_all()
    assert session.get_one(DocumentRow, PEDIA_REV).linked_at is None  # continua por ver
    assert run_linker(services).documents == 1  # e vê-se com o alias novo


def test_com_atraso_a_execucao_avisa_e_o_runner_volta_logo(
    services: Services, session: Session
) -> None:
    for n in range(5):
        add_doc(services, session, f"{WIKIBOOKS}/Doc{n}", f"Documento número {n}")
    services.settings.link_batch = 2
    services.settings.link_interval_seconds = 3600
    runner = Runner(services, ["linker"], worker_id="teste")

    runner.housekeeping()

    assert (
        runner._next_periodic["linker"] - time.monotonic() <= services.settings.worker_poll_seconds
    )
    runner._next_periodic["linker"] = 0.0
    runner.housekeeping()
    runner._next_periodic["linker"] = 0.0
    runner.housekeeping()  # 5 documentos, 2 por execução: a 3.ª execução esvazia
    assert (
        runner._next_periodic["linker"] - time.monotonic() > 3000
    )  # sem atraso: espera o intervalo
    assert run_linker(services).documents == 0


def test_run_linker_marca_backlog_so_se_parou_no_limite(
    services: Services, session: Session
) -> None:
    for n in range(3):
        add_doc(services, session, f"{WIKIBOOKS}/Doc{n}", f"Documento número {n}")
    services.settings.link_batch = 2

    assert run_linker(services).backlog is True
    assert run_linker(services).backlog is False  # o último documento
    stop = threading.Event()
    stop.set()
    assert run_linker(services, stop).backlog is False


# --- kNN dos vetores de artigo ----------------------------------------------------------------


def seed_many(services: Services, session: Session, count: int) -> list[str]:
    ids = [f"{WIKIBOOKS}/N{n:03d}" for n in range(count)]
    for document_id in ids:
        add_doc(services, session, document_id)
    services.store.put_article_vectors(
        session,
        MODEL,
        {d: unit_vector(0.99 - n * 1e-4, 2 + n % 300) for n, d in enumerate(ids)},
    )
    add_doc(services, session, A, vector=unit_vector(1.0))
    return ids


def test_vizinhos_semanticos_passam_do_limite_de_40_do_hnsw(
    services: Services, session: Session
) -> None:
    ids = seed_many(services, session, 120)

    found = services.store.nearest_documents(session, A, MODEL, 100)

    assert [d for d, _ in found] == ids[:100]  # do mais para o menos parecido, sem parar nos 40


def test_vizinhos_semanticos_ignoram_rejeitados_e_fontes_que_sumiram(
    services: Services, session: Session
) -> None:
    ids = seed_many(services, session, 6)
    first, second = ids[0], ids[1]
    session.add(Link(src_document=first, dst_document=A, kind="manual", status="rejected"))
    session.add(Link(src_document=ids[2], dst_document=A, kind="vector", status="accepted"))
    session.commit()

    assert services.store.similar_documents(session, A, MODEL, 3) == ids[1:4]
    assert second in [d for d, _ in services.store.nearest_documents(session, A, MODEL, 3)]

    session.execute(update(Source).where(Source.id == WIKIBOOKS).values(status="missing"))
    session.commit()

    assert services.store.nearest_documents(session, A, MODEL, 3) == []


def test_aprofunda_tambem_nas_ligacoes_vetoriais_com_rejeitados(
    services: Services, session: Session
) -> None:
    add_doc(services, session, B, vector=unit_vector(0.97, 1))
    add_doc(services, session, C, vector=unit_vector(0.96, 2))
    add_doc(services, session, A, vector=unit_vector(1.0))
    session.add(Link(src_document=B, dst_document=A, kind="vector", status="rejected"))
    session.commit()

    found = services.store.similar_across_sources(session, A, MODEL, 0.88, 1)

    assert [d for d, _ in found] == [C]  # o rejeitado não gasta o lugar


def test_aresta_nao_dirigida_fora_de_ordem_e_recusada_pela_base_de_dados(
    services: Services, session: Session
) -> None:
    from sqlalchemy.exc import IntegrityError

    add_doc(services, session, PEDIA_REV, "Revolução dos Cravos")
    add_doc(services, session, BOOKS_REV, "Revolução dos Cravos")

    # zim_internal tem sentido (A cita B), em qualquer ordem; as outras guardam-se com src < dst
    session.add(Link(src_document=PEDIA_REV, dst_document=BOOKS_REV, kind="zim_internal"))
    session.commit()
    session.add(Link(src_document=PEDIA_REV, dst_document=BOOKS_REV, kind="vector"))
    with pytest.raises(IntegrityError, match="ck_link_undirected_order"):
        session.commit()
    session.rollback()
    session.add(Link(src_document=BOOKS_REV, dst_document=PEDIA_REV, kind="vector"))
    session.commit()
