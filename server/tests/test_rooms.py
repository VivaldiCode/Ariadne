"""Fase 9: cálculo das salas (k-means sobre os vetores de artigo), atribuição incremental e os
recálculos que mantêm os ids."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import DocumentVector, Room
from ariadne.embedder import FakeEmbedder
from ariadne.rooms import list_rooms, rooms_by_wing, rooms_of
from ariadne.rooms_build import (
    assign_pending,
    build_wing,
    make_label,
    needs_build,
    run_rooms,
    wing_counts,
    wing_lock,
)
from ariadne.services import Services
from tests.fakes import FakeKiwix, default_fake
from tests.roomfactory import MODEL, cluster_vector, seed_documents
from tests.test_level3 import SOURCE, make_services

pytestmark = pytest.mark.db

WING = "wikipedia_pt_teste"
GROUPS = {
    0: ["Fado", "Guitarra", "Amália", "Saudade"],
    1: ["Lisboa", "Porto", "Coimbra", "Faro"],
    2: ["Átomo", "Molécula", "Protão", "Neutrão"],
}


@pytest.fixture
def services(db: sessionmaker[Session], zim_dir: Path) -> Iterator[Services]:
    fake: FakeKiwix = default_fake()
    services = make_services(
        db,
        zim_dir,
        fake,
        FakeEmbedder(),
        rooms_min=3,
        rooms_max=3,  # três grupos nos dados: três salas
        rooms_min_documents=6,
    )
    yield services
    services.close()


@pytest.fixture
def session(db: sessionmaker[Session]) -> Iterator[Session]:
    with db() as s:
        yield s


def room_of(session: Session, title: str) -> int | None:
    session.expire_all()
    return session.scalar(select(DocumentRow.room).where(DocumentRow.id == f"{SOURCE}/{title}"))


def test_cada_grupo_fica_na_sua_sala_com_o_rotulo_dos_titulos_centrais(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)

    report = build_wing(services, WING, MODEL)

    assert (report.documents, report.rooms, report.created, report.kept) == (12, 3, 3, 0)
    for titles in GROUPS.values():
        assert len({room_of(session, t) for t in titles}) == 1
    assert len({room_of(session, titles[0]) for titles in GROUPS.values()}) == 3
    rooms = {r.id: r for r in session.scalars(select(Room))}
    fado = rooms[room_of(session, "Fado") or 0]
    # O 1.º título de cada grupo é o mais central; o rótulo são os 3 mais perto do centróide.
    assert fado.label.split(" · ")[0] == "Fado"
    assert set(fado.label.split(" · ")) <= set(GROUPS[0])
    assert len(fado.label.split(" · ")) == 3
    assert (fado.size, fado.base_size, fado.wing, fado.model_id) == (4, 4, WING, MODEL)


def test_recalcular_com_os_mesmos_dados_mantem_ids_e_atribuicoes(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    build_wing(services, WING, MODEL)
    before = {t: room_of(session, t) for ts in GROUPS.values() for t in ts}
    ids = sorted(session.scalars(select(Room.id)))

    report = build_wing(services, WING, MODEL)

    assert (report.created, report.kept, report.removed) == (0, 3, 0)
    assert sorted(session.scalars(select(Room.id))) == ids
    assert {t: room_of(session, t) for ts in GROUPS.values() for t in ts} == before


def test_resultado_e_determinista_entre_bases(services: Services, session: Session) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    build_wing(services, WING, MODEL)
    first = sorted(session.scalars(select(Room.label)))
    build_wing(services, WING, MODEL)
    session.expire_all()
    assert sorted(session.scalars(select(Room.label))) == first


def test_documento_novo_vai_para_a_sala_do_centroide_mais_proximo(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    build_wing(services, WING, MODEL)
    seed_documents(services, session, SOURCE, {2: ["Eletrão"]})
    assert room_of(session, "Eletrão") is None

    assigned = assign_pending(services, WING, MODEL)

    assert assigned == 1
    assert room_of(session, "Eletrão") == room_of(session, "Átomo")
    session.expire_all()
    atomo = session.get(Room, room_of(session, "Átomo"))
    assert atomo is not None
    assert (atomo.size, atomo.base_size) == (5, 4)  # o tamanho cresce, a base não


def test_assign_pending_sem_salas_nao_faz_nada(services: Services, session: Session) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    assert assign_pending(services, WING, MODEL) == 0
    assert room_of(session, "Fado") is None


def test_ala_pequena_demais_fica_sem_salas(services: Services, session: Session) -> None:
    seed_documents(services, session, SOURCE, {0: ["Fado", "Guitarra"], 1: ["Lisboa"]})

    report = build_wing(services, WING, MODEL)

    assert report.skipped is not None
    assert session.scalar(select(Room.id)) is None


def test_modelo_novo_substitui_as_salas_antigas_sem_herdar_ids(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    build_wing(services, WING, MODEL)
    old_ids = set(session.scalars(select(Room.id)))
    # Os mesmos documentos com os vetores de outro modelo.
    for vector in session.scalars(select(DocumentVector)):
        session.add(
            DocumentVector(
                document_id=vector.document_id, model_id="outro@1", embedding=vector.embedding
            )
        )
    session.commit()

    report = build_wing(services, WING, "outro@1")

    session.expire_all()
    assert (report.created, report.kept, report.removed) == (3, 0, 3)
    new_ids = set(session.scalars(select(Room.id)))
    assert new_ids.isdisjoint(old_ids)
    assert {r.model_id for r in session.scalars(select(Room))} == {"outro@1"}


def test_necessidade_de_recalculo_segue_o_crescimento(services: Services) -> None:
    settings = services.settings
    assert not needs_build(settings, documents=5, rooms=0, base=0)  # poucos
    assert needs_build(settings, documents=12, rooms=0, base=0)  # nunca calculadas
    assert not needs_build(settings, documents=12, rooms=3, base=12)
    assert not needs_build(settings, documents=14, rooms=3, base=12)  # +16 %
    assert needs_build(settings, documents=15, rooms=3, base=12)  # +25 %


def test_ronda_calcula_atribui_e_recalcula_quando_a_ala_cresce(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)

    first = run_rooms(services)
    assert [(w.wing, w.rebuilt, w.rooms) for w in first.wings] == [(WING, True, 3)]
    assert wing_counts(session, MODEL) == {WING: 12}

    seed_documents(services, session, SOURCE, {1: ["Braga"]})  # +8 %: só atribui
    second = run_rooms(services)
    assert [(w.rebuilt, w.assigned) for w in second.wings] == [(False, 1)]
    assert room_of(session, "Braga") == room_of(session, "Lisboa")

    seed_documents(services, session, SOURCE, {1: ["Évora", "Viseu"], 0: ["Viola"]})  # +31 %
    third = run_rooms(services)
    assert [(w.rebuilt, w.kept, w.created) for w in third.wings] == [(True, 3, 0)]
    assert room_of(session, "Viola") == room_of(session, "Fado")


def test_force_recalcula_mesmo_sem_crescimento_e_wing_limita(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    run_rooms(services)

    assert [w.rebuilt for w in run_rooms(services).wings] == [False]
    assert [w.rebuilt for w in run_rooms(services, force=True).wings] == [True]
    assert run_rooms(services, wing="outra-ala", force=True).wings == []


def test_ronda_sem_embedder_espera(services: Services, session: Session) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    services.embedder = None  # o embedder volta a ser o de `settings`...
    services.settings.embedder = "http"  # ...que aqui não tem URL: não se configura
    assert run_rooms(services).wings == []


def test_paragem_pedida_nao_deixa_salas_a_meio(services: Services, session: Session) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    stop = threading.Event()
    stop.set()
    assert run_rooms(services, stop).wings == []


def test_consultas_listam_salas_por_ala_e_dao_a_sala_de_cada_documento(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    build_wing(services, WING, MODEL)

    listed = list_rooms(session, wing=WING)
    assert [r.size for r in listed] == [4, 4, 4]
    assert list_rooms(session, wing="outra") == []
    assert list_rooms(session, wing=WING, model_id="outro") == []
    assert len(list_rooms(session, limit=2)) == 2

    by_wing = rooms_by_wing(session, [WING, "outra"], top=2)
    assert by_wing[WING].count == 3
    assert len(by_wing[WING].top) == 2
    assert "outra" not in by_wing

    refs = rooms_of(session, [f"{SOURCE}/Fado", f"{SOURCE}/Inexistente"])
    assert list(refs) == [f"{SOURCE}/Fado"]
    assert refs[f"{SOURCE}/Fado"].label.startswith("Fado")


def test_nunca_grava_salas_vazias(services: Services, session: Session) -> None:
    # 7 artigos com o vetor do grupo 0 e 7 com o do grupo 1: pedir 3 salas dava uma vazia.
    seed_documents(services, session, SOURCE, {0: [f"A{n}" for n in range(7)]})
    seed_documents(services, session, SOURCE, {1: [f"B{n}" for n in range(7)]})
    for vector in session.scalars(select(DocumentVector)):
        vector.embedding = cluster_vector(0 if "/A" in vector.document_id else 1)  # iguais dentro
    session.commit()

    report = build_wing(services, WING, MODEL)

    session.expire_all()
    sizes = sorted(r.size for r in session.scalars(select(Room)))
    assert 0 not in sizes and sum(sizes) == 14
    assert report.rooms == len(sizes) == 2
    assert session.scalar(select(func.count()).select_from(Room).where(Room.ready.is_(False))) == 0


def test_salas_novas_so_aparecem_quando_o_calculo_acaba(
    services: Services, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    seen: list[tuple[int, list[str]]] = []
    from ariadne import rooms_build

    original = rooms_build._write_rooms

    def spy(svc: Services, ids: list[str], rooms: list[int]) -> None:
        original(svc, ids, rooms)
        with svc.sessions() as other:  # outra sessão, a meio do cálculo
            listed = list_rooms(other, wing=WING)
            seen.append((len(listed), [r.label for r in listed]))
            assert rooms_of(other, ids) == {}  # os documentos ainda não mostram a sala nova
            assert other.scalar(select(func.count()).select_from(Room)) == 3  # existem, escondidas

    monkeypatch.setattr(rooms_build, "_write_rooms", spy)

    build_wing(services, WING, MODEL)

    assert seen and all(listed == (0, []) for listed in seen)  # nenhuma com rótulo vazio
    assert [r.label != "" for r in list_rooms(session, wing=WING)] == [True, True, True]


def test_recalcular_so_mexe_no_updated_at_quando_ha_mudanca(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    build_wing(services, WING, MODEL)
    stamps = {r.id: r.updated_at for r in session.scalars(select(Room))}
    session.expire_all()

    build_wing(services, WING, MODEL)  # os mesmos dados: nada mudou
    seed_documents(services, session, SOURCE, {2: ["Eletrão"]})
    assign_pending(services, WING, MODEL)  # só os tamanhos mudam

    session.expire_all()
    assert {r.id: r.updated_at for r in session.scalars(select(Room))} == stamps
    assert sorted(r.size for r in session.scalars(select(Room))) == [4, 4, 5]


def test_mudar_de_modelo_so_substitui_as_salas_com_cobertura_suficiente(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)
    run_rooms(services)
    old_ids = set(session.scalars(select(Room.id)))
    # O embedder passa a outro modelo; só 6 dos 12 artigos já têm vetor dele (50 %).
    services.embedder = FakeEmbedder(model_id="outro@1")
    for vector in list(session.scalars(select(DocumentVector)))[:6]:
        session.add(
            DocumentVector(
                document_id=vector.document_id, model_id="outro@1", embedding=vector.embedding
            )
        )
    session.commit()

    kept = run_rooms(services)

    assert kept.wings[0].skipped is not None and "50%" in kept.wings[0].skipped
    assert set(session.scalars(select(Room.id))) == old_ids  # as antigas ficam
    assert len(list_rooms(session, wing=WING)) == 3

    for vector in list(session.scalars(select(DocumentVector)))[:12]:
        if vector.model_id == MODEL:
            session.merge(
                DocumentVector(
                    document_id=vector.document_id, model_id="outro@1", embedding=vector.embedding
                )
            )
    session.commit()
    switched = run_rooms(services)  # 100 %: substitui

    session.expire_all()
    assert switched.wings[0].rebuilt
    assert {r.model_id for r in session.scalars(select(Room))} == {"outro@1"}
    assert set(session.scalars(select(Room.id))).isdisjoint(old_ids)


def test_ala_ocupada_regista_que_esta_a_calcular_noutro_processo(
    services: Services, session: Session
) -> None:
    seed_documents(services, session, SOURCE, GROUPS)

    with wing_lock(services, WING) as got:
        assert got
        run = run_rooms(services)  # outro processo (aqui, outra ligação) tenta a mesma ala

    [report] = run.wings
    assert report.busy and report.skipped == "a calcular noutro processo"
    assert session.scalar(select(Room.id)) is None  # nada se calculou
    assert [w.rebuilt for w in run_rooms(services).wings] == [True]  # livre: calcula


def test_rotulo_sem_repeticoes_e_com_limite() -> None:
    assert make_label(["A", "A", " B  b ", "C", "D"]) == "A · B b · C"
    assert make_label([]) == "(untitled)"
    assert len(make_label(["x" * 400])) <= 160
    assert cluster_vector(0)[0] == 1.0
