"""Fase 9: salas na pesquisa — filtros `rooms`/`wings`, a sala nos resultados, o bónus de fusão."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, create_principal
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import QueryLog, Room
from ariadne.documents import read_document
from ariadne.embedder import FakeEmbedder
from ariadne.rooms import RoomRef
from ariadne.router import Candidate, fuse
from ariadne.search import SearchError, search
from ariadne.services import Services
from tests.fakes import FakeKiwix, default_fake
from tests.test_level3 import (
    LISBOA,
    OUTRO,
    PORTUGAL,
    QUERY,
    QUERY_VECTOR,
    SOURCE,
    make_document,
    make_services,
    put_chunk,
    unit_vector,
)

pytestmark = pytest.mark.db

MODEL = "fake/embedder@0000000"
WING = "wikipedia_pt_teste"


@pytest.fixture
def fake() -> FakeKiwix:
    return default_fake()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(
        vectors={
            QUERY: QUERY_VECTOR,
            # A ordem do fallback (kiwix: Lisboa, Portugal, Outro) fica como o kiwix a deu.
            "Lisboa. excerto de Lisboa": unit_vector(0.9),
            "Portugal. excerto de Portugal": unit_vector(0.5),
            "Outro. excerto de Outro": unit_vector(0.2),
        }
    )


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


def make_room(session: Session, label: str, centroid: list[float]) -> int:
    room = Room(wing=WING, label=label, model_id=MODEL, centroid=centroid)
    session.add(room)
    session.commit()
    return room.id


def put_in_room(services: Services, session: Session, room_id: int, *paths: str) -> None:
    for path in paths:  # os documentos só existem na base depois de vistos
        make_document(services, session, path)
    session.execute(
        update(DocumentRow)
        .where(DocumentRow.id.in_([f"{SOURCE}/{p}" for p in paths]))
        .values(room=room_id)
    )
    session.commit()


def test_resultados_trazem_a_ala_e_a_sala(
    services: Services, session: Session, principal_id: int
) -> None:
    put_chunk(services, session, "Lisboa", 0.92)
    cidades = make_room(session, "Lisboa · Porto", unit_vector(1.0))
    put_in_room(services, session, cidades, "Lisboa")

    result = search(services, session, principal_id, QUERY, k=2)

    by_id = {h.document_id: h for h in result.results}
    assert by_id[LISBOA].room == RoomRef(id=cidades, label="Lisboa · Porto")
    assert by_id[LISBOA].wing == WING
    assert by_id[PORTUGAL].room is None  # sem sala calculada
    assert by_id[PORTUGAL].wing == WING


def test_filtro_por_sala_so_devolve_documentos_dessa_sala(
    services: Services, session: Session, principal_id: int, fake: FakeKiwix
) -> None:
    put_chunk(services, session, "Lisboa", 0.92)
    put_chunk(services, session, "Outro", 0.90)
    sala_a = make_room(session, "A", unit_vector(1.0))
    sala_b = make_room(session, "B", unit_vector(0.5, 3))
    put_in_room(services, session, sala_a, "Lisboa")
    put_in_room(services, session, sala_b, "Outro")

    only_a = search(services, session, principal_id, QUERY, rooms=[sala_a])
    only_b = search(services, session, principal_id, QUERY, rooms=[sala_b])
    both = search(services, session, principal_id, QUERY, rooms=[sala_a, sala_b])

    # O fallback (kiwix) também dá Portugal, que não tem sala: fica de fora.
    assert [h.document_id for h in only_a.results] == [LISBOA]
    assert [h.document_id for h in only_b.results] == [OUTRO]
    assert {h.document_id for h in both.results} == {LISBOA, OUTRO}
    log = session.get(QueryLog, only_a.query_id)
    assert log is not None
    assert log.filters["rooms"] == [sala_a] and log.filters["wings"] == [WING]


def test_filtro_por_sala_tambem_filtra_o_fallback(
    services: Services, session: Session, principal_id: int
) -> None:
    """Sem nenhum chunk indexado a pesquisa vem do kiwix; fica só o que está na sala."""
    sala = make_room(session, "Portugal", unit_vector(1.0))
    result_before = search(services, session, principal_id, QUERY, rooms=[sala])
    assert result_before.results == []  # o kiwix deu três documentos, nenhum com sala

    put_in_room(
        services, session, sala, "Portugal"
    )  # (o documento passou a existir ao ser mostrado)
    result = search(services, session, principal_id, QUERY, rooms=[sala])

    assert [h.document_id for h in result.results] == [PORTUGAL]
    assert result.results[0].level == 1


def test_sala_inexistente_ou_de_outra_ala_e_recusada(
    services: Services, session: Session, principal_id: int
) -> None:
    sala = make_room(session, "A", unit_vector(1.0))
    with pytest.raises(SearchError, match="Rooms do not exist"):
        search(services, session, principal_id, QUERY, rooms=[sala + 999])
    with pytest.raises(SearchError, match="outside the requested wings"):
        search(services, session, principal_id, QUERY, rooms=[sala], wings=["outra-ala"])


def test_filtro_por_ala(services: Services, session: Session, principal_id: int) -> None:
    ok = search(services, session, principal_id, QUERY, wings=[WING])
    assert {h.wing for h in ok.results} == {WING}
    with pytest.raises(SearchError, match="None of the requested wings exist"):
        search(services, session, principal_id, QUERY, wings=["nao-existe"])


def test_bonus_de_sala_desempata_a_favor_da_sala_provavel(
    services: Services, session: Session, principal_id: int
) -> None:
    put_chunk(services, session, "Chunk_isolado", 0.82)  # zona cinzenta: nível 3 + kiwix fundidos
    provavel = make_room(session, "Provável", unit_vector(1.0))  # centróide ≈ pergunta
    longe = make_room(session, "Longe", unit_vector(0.1, 5))
    put_in_room(services, session, provavel, "Outro")
    put_in_room(services, session, longe, "Lisboa")
    services.settings.router_room_bonus = 0.0
    sem_bonus = search(services, session, principal_id, QUERY)
    services.settings.router_room_bonus = 0.002
    services.settings.router_rooms_top = 1  # só a sala mais perto da pergunta conta

    com_bonus = search(services, session, principal_id, QUERY)

    sem = [h.document_id for h in sem_bonus.results if h.level == 1]
    com = [h.document_id for h in com_bonus.results if h.level == 1]
    assert sem == [LISBOA, PORTUGAL, OUTRO]  # a ordem do kiwix/embedding
    assert com == [OUTRO, LISBOA, PORTUGAL]  # Outro (sala provável) sobe; Lisboa (longe) não


def test_fuse_soma_o_bonus_so_a_quem_esta_nas_listas() -> None:
    from tests.test_level3 import SOURCE as _  # noqa: F401 (o módulo já está importado)

    class _S:  # a fonte só precisa de `id`
        id = "s"

    a = Candidate(_S(), "a", "A", "", level=1, score=1)  # type: ignore[arg-type]
    b = Candidate(_S(), "b", "B", "", level=1, score=0.5)  # type: ignore[arg-type]

    plain = fuse(None, None, [], [a, b], 2)
    boosted = fuse(None, None, [], [a, b], 2, room_bonus={("s", "b"): 0.01, ("s", "x"): 0.5})

    assert [c.path for c in plain] == ["a", "b"]
    assert [c.path for c in boosted] == ["b", "a"]
    assert boosted[0].score == pytest.approx(1 / 62 + 0.01)


def test_ler_um_documento_mostra_a_ala_e_a_sala(
    services: Services, session: Session, principal_id: int
) -> None:
    sala = make_room(session, "Países", unit_vector(1.0))
    view_before = read_document(services, session, principal_id, PORTUGAL)
    assert (view_before.wing, view_before.room) == (WING, None)

    put_in_room(services, session, sala, "Portugal")
    view = read_document(services, session, principal_id, PORTUGAL)

    assert view.room == RoomRef(id=sala, label="Países")


def test_pergunta_abaixo_do_limiar_baixo_com_sala_mantem_os_candidatos_da_sala(
    services: Services, session: Session, principal_id: int
) -> None:
    put_chunk(services, session, "Lisboa", 0.70)  # parecido, mas abaixo de τ_baixo (0,80)
    sala = make_room(session, "Cidades", unit_vector(1.0))
    put_in_room(services, session, sala, "Lisboa")

    sem_sala = search(services, session, principal_id, QUERY, k=3)
    com_sala = search(services, session, principal_id, QUERY, k=3, rooms=[sala])

    assert all(h.level == 1 for h in sem_sala.results)  # sem sala: só o fallback (como antes)
    assert [(h.document_id, h.level) for h in com_sala.results] == [(LISBOA, 3)]
    assert com_sala.results[0].room is not None


def test_ids_de_sala_fora_do_intervalo_do_bigint_sao_recusados(
    services: Services, session: Session, principal_id: int
) -> None:
    for bad in (10**30, 0, -1, 2**63):
        with pytest.raises(SearchError, match="Invalid room id"):
            search(services, session, principal_id, QUERY, rooms=[bad])
    sala = make_room(session, "A", unit_vector(1.0))
    with pytest.raises(SearchError, match="Rooms do not exist"):
        search(services, session, principal_id, QUERY, rooms=[2**63 - 1])
    assert sala < 2**63 - 1
