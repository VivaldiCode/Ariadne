"""Fase 7: a regra histórica do router — a versão na língua original entra com o selo."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, create_principal
from ariadne.concepts import origin
from ariadne.concepts.importers import import_sitelinks
from ariadne.db.models import Curation, Link, QueryLog, QueryResult
from ariadne.db.models import Document as DocumentRow
from ariadne.embedder import FakeEmbedder
from ariadne.search import SearchResult, search
from ariadne.services import Services
from tests import sitelinks
from tests.fakes import EN_BOOK, PT_BOOK, FakeKiwix
from tests.langzims import BOOKS_CRAVOS, EN_CRAVOS, EN_LISBON, PT_CRAVOS, PT_LISBOA, make_lang_dir
from tests.test_level3 import make_services

pytestmark = pytest.mark.db

QID = f"Q{sitelinks.Q_CRAVOS}"
QUERY = "When was the Carnation Revolution in 1974?"


def fake_kiwix() -> FakeKiwix:
    return FakeKiwix(
        {
            EN_BOOK: [("Carnation_Revolution", "Carnation Revolution"), ("Lisbon", "Lisbon")],
            PT_BOOK: [],
        }
    )


def build(db: sessionmaker[Session], tmp_path: Path, **overrides: object) -> Services:
    services = make_services(
        db, make_lang_dir(tmp_path / "zim"), fake_kiwix(), FakeEmbedder(), **overrides
    )
    dump = sitelinks.write_items_dump(tmp_path / "items.sql.gz")
    import_sitelinks(db, services.registry.active(), dump)
    return services


@pytest.fixture
def services(db: sessionmaker[Session], tmp_path: Path) -> Iterator[Services]:
    services = build(db, tmp_path)
    yield services
    services.close()


@pytest.fixture
def principal_id(db: sessionmaker[Session]) -> int:
    with db() as session, session.begin():
        return create_principal(session, PrincipalKind.agent, "agente")[0].id


def set_origin(db: sessionmaker[Session], qid: str, lang: str | None) -> None:
    with db() as session, session.begin():
        origin.set_origin(session, qid, lang)


def ask(
    services: Services, db: sessionmaker[Session], principal_id: int, query: str, **kwargs: object
) -> SearchResult:
    with db() as session:
        return search(services, session, principal_id, query, **kwargs)  # type: ignore[arg-type]


def ids(result: SearchResult) -> list[str]:
    return [hit.document_id for hit in result.results]


def test_pergunta_historica_em_ingles_acrescenta_a_versao_pt_com_selo(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    set_origin(db, QID, "pt")

    result = ask(services, db, principal_id, QUERY)

    assert ids(result) == [EN_CRAVOS, PT_CRAVOS, EN_LISBON]  # logo a seguir ao que a disparou
    en, pt, lisbon = result.results
    assert (en.original_source, pt.original_source, lisbon.original_source) == (False, True, False)
    assert pt.language == "pt" and pt.title == "Revolução dos Cravos"
    assert (
        pt.level == 1 and pt.score == en.score
    )  # o nível onde o texto está (sem página no Wiki.js)
    assert "golpe militar" in pt.snippet  # o 1.º parágrafo da versão original
    assert (
        pt.kiwix_url
        == "http://kiwix.publico:8801/content/wikipedia_pt_teste_2026-09/Revolucao_dos_Cravos"
    )
    with db() as session:
        flags = {
            r.document_id: r.original_source
            for r in session.scalars(
                select(QueryResult).where(QueryResult.query_id == result.query_id)
            )
        }
        log = session.get(QueryLog, result.query_id)
    assert flags == {EN_CRAVOS: False, PT_CRAVOS: True, EN_LISBON: False}
    assert log is not None and log.filters is not None
    assert (
        "keyword:revolution" in log.filters["historical"]
        and "year:1974" in log.filters["historical"]
    )


def test_pergunta_nao_historica_nao_acrescenta(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    set_origin(db, QID, "pt")
    set_origin(db, "Q77", "pt")

    result = ask(services, db, principal_id, "Lisbon sights and food")

    assert ids(result) == [EN_CRAVOS, EN_LISBON]
    assert not any(hit.original_source for hit in result.results)
    with db() as session:
        log = session.get(QueryLog, result.query_id)
    assert log is not None and log.filters is not None and "historical" not in log.filters


def test_sem_lingua_original_conhecida_nao_se_inventa(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    result = ask(services, db, principal_id, QUERY)  # origin_lang é NULL

    assert ids(result) == [EN_CRAVOS, EN_LISBON]
    assert not any(hit.original_source for hit in result.results)


def test_pergunta_na_lingua_original_nao_acrescenta_nada(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    set_origin(db, QID, "pt")
    services.kiwix = FakeKiwix(
        {PT_BOOK: [("Revolucao_dos_Cravos", "Revolução dos Cravos")]}
    ).client()

    result = ask(services, db, principal_id, "Quando foi a Revolução dos Cravos em 1974?")

    assert ids(result) == [PT_CRAVOS]
    assert not result.results[0].original_source  # a pergunta já está na língua original


def test_a_versao_original_que_ja_esta_nos_resultados_so_se_assinala(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    set_origin(db, QID, "pt")
    services.kiwix = FakeKiwix(
        {
            EN_BOOK: [("Carnation_Revolution", "Carnation Revolution")],
            PT_BOOK: [("Revolucao_dos_Cravos", "Revolução dos Cravos"), ("Lisboa", "Lisboa")],
        }
    ).client()

    result = ask(services, db, principal_id, QUERY)

    assert sorted(ids(result)) == sorted([EN_CRAVOS, PT_CRAVOS, PT_LISBOA])  # sem duplicados
    assert {hit.document_id: hit.original_source for hit in result.results} == {
        EN_CRAVOS: False,
        PT_CRAVOS: True,
        PT_LISBOA: False,  # Lisboa não tem língua original conhecida
    }


def test_filtros_da_pesquisa_limitam_as_versoes_acrescentadas(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    set_origin(db, QID, "pt")

    by_lang = ask(services, db, principal_id, "Revolução em 1974", lang="en")
    by_source = ask(services, db, principal_id, QUERY, sources=["zim:wikipedia_en_teste"])

    for result in (by_lang, by_source):  # o PT não está nas fontes pedidas: não entra
        assert ids(result) == [EN_CRAVOS, EN_LISBON]
        assert not any(hit.original_source for hit in result.results)


def test_o_total_nunca_passa_de_k_e_a_original_ocupa_o_lugar_do_ultimo(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    set_origin(db, QID, "pt")

    two = ask(services, db, principal_id, QUERY, k=2)
    one = ask(services, db, principal_id, QUERY, k=1)

    assert ids(two) == [EN_CRAVOS, PT_CRAVOS]  # a Lisbon saiu; o 1.º resultado fica
    assert two.results[1].original_source
    assert ids(one) == [EN_CRAVOS]  # com k=1 não há lugar: não se desloca o 1.º


def test_aresta_de_lingua_rejeitada_nao_traz_a_versao(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    set_origin(db, QID, "pt")
    with db() as session, session.begin():
        session.execute(
            update(Link)
            .where(
                Link.kind.in_(["langlink", "wikidata"]),
                or_(Link.src_document == EN_CRAVOS, Link.dst_document == EN_CRAVOS),
            )
            .values(status="rejected")
        )

    result = ask(services, db, principal_id, QUERY)

    assert ids(result) == [EN_CRAVOS, EN_LISBON]


def curate(db: sessionmaker[Session], decision: str, archived: bool = False) -> None:
    with db() as session, session.begin():
        session.add(
            Curation(
                document_id=PT_CRAVOS,
                source_version="2026-09",
                fingerprint="x",
                decision=decision,
                original_md="texto",
                archived_at=datetime.now(UTC) if archived else None,
            )
        )
        session.execute(
            update(DocumentRow).where(DocumentRow.id == PT_CRAVOS).values(wiki_page_id=9)
        )


@pytest.mark.parametrize(("decision", "archived"), [("rejected", False), ("published", True)])
def test_item_arquivado_ou_rejeitado_na_curadoria_nao_entra(
    services: Services,
    db: sessionmaker[Session],
    principal_id: int,
    decision: str,
    archived: bool,
) -> None:
    set_origin(db, QID, "pt")
    curate(db, decision, archived)

    found = ids(ask(services, db, principal_id, QUERY))

    assert PT_CRAVOS not in found  # o item da Wikipédia PT não entra; os Wikilivros PT, sim
    assert found == [EN_CRAVOS, BOOKS_CRAVOS, EN_LISBON]


def test_nivel_2_so_com_pagina_valida(
    services: Services, db: sessionmaker[Session], principal_id: int
) -> None:
    set_origin(db, QID, "pt")
    with db() as session, session.begin():  # wiki_page_id sem curadoria: não é uma página válida
        session.execute(
            update(DocumentRow).where(DocumentRow.id == PT_CRAVOS).values(wiki_page_id=9)
        )
    assert ask(services, db, principal_id, QUERY).results[1].level == 1

    curate(db, "published")  # com a curadoria publicada, o nível 2 é verdade
    assert ask(services, db, principal_id, QUERY).results[1].level == 2


def test_regra_desligada_por_configuracao(
    db: sessionmaker[Session], tmp_path: Path, principal_id: int
) -> None:
    services = build(db, tmp_path, router_historical_rule=False)
    try:
        set_origin(db, QID, "pt")

        result = ask(services, db, principal_id, QUERY)

        assert ids(result) == [EN_CRAVOS, EN_LISBON]
    finally:
        services.close()
