"""Fase 6: o nível 2 no router e a escolha do que importar (varrimento seletivo)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ariadne import heat
from ariadne.auth import PrincipalKind, create_principal
from ariadne.db.models import Job
from ariadne.embedder import FakeEmbedder
from ariadne.search import search
from ariadne.services import Services
from ariadne.wiki.base import WikiHit
from ariadne.workers.importer import run_import_scan
from tests.curationzims import BOOKS_REV, LISBOA, MALICIOSO, PORTUGAL, REMEDIO, make_dir
from tests.fakes import default_fake
from tests.test_curation import ensure_docs, link, request_import, run_jobs
from tests.test_level3 import make_services
from tests.wikifakes import FakeWiki

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]


@pytest.fixture
def services(db: sessionmaker[Session], tmp_path: Path) -> Iterator[Services]:
    zims = make_dir(tmp_path / "zims")
    services = make_services(
        db,
        zims,
        default_fake(),
        FakeEmbedder(),
        wikijs_url="http://wiki",
        wikijs_token="t",
        wikijs_public_url="http://wiki.publico:8802",
    )
    services.wiki = FakeWiki()
    yield services
    services.close()


@pytest.fixture
def principal_id(db: sessionmaker[Session]) -> int:
    with db() as session, session.begin():
        principal, _ = create_principal(session, PrincipalKind.agent, "agente-de-teste")
        return principal.id


def fake_wiki(services: Services) -> FakeWiki:
    assert isinstance(services.wiki, FakeWiki)
    return services.wiki


def import_now(services: Services, *document_ids: str) -> None:
    request_import(services, *document_ids)
    run_jobs(services)


# --- router --------------------------------------------------------------------------------------


def test_resultados_do_wiki_entram_na_fusao_com_nivel_2_e_wiki_url(
    services: Services, principal_id: int, db: sessionmaker[Session]
) -> None:
    import_now(services, PORTUGAL)
    with db() as session:
        page_id = session.execute(
            text("SELECT wiki_page_id FROM document WHERE id = :id"), {"id": PORTUGAL}
        ).scalar_one()
        path = session.execute(
            text("SELECT meta->>'wiki_path' FROM document WHERE id = :id"), {"id": PORTUGAL}
        ).scalar_one()
        fake_wiki(services).hits = [
            WikiHit(page_id, path, "Portugal", "Portugal é um país da Europa."),
            WikiHit(999, "outra/coisa", "Estranha", "não é nossa"),  # sem documento: ignorada
        ]

        result = search(services, session, principal_id, "capital", k=5)

    by_id = {h.document_id: h for h in result.results}
    assert result.results[0].document_id == PORTUGAL  # está nas duas listas: sobe no RRF
    assert by_id[PORTUGAL].level == 2
    assert by_id[PORTUGAL].wiki_url == f"http://wiki.publico:8802/{path}"
    assert by_id[PORTUGAL].snippet == "Portugal é um país da Europa."
    assert by_id[LISBOA].level == 1 and by_id[LISBOA].wiki_url is None
    assert all("Estranha" not in h.title for h in result.results)
    assert result.warnings == []


def test_wiki_em_baixo_degrada_sem_erro(
    services: Services, principal_id: int, db: sessionmaker[Session]
) -> None:
    fake_wiki(services).down = True

    with db() as session:
        result = search(services, session, principal_id, "capital", k=3)

    assert [h.level for h in result.results] == [1, 1, 1]
    assert any("Wiki.js unavailable" in w for w in result.warnings)


def test_so_o_wiki_responde_quando_o_kiwix_falha(
    services: Services, principal_id: int, db: sessionmaker[Session]
) -> None:
    import_now(services, PORTUGAL)
    fake = default_fake()
    fake.down = {"wikipedia_pt_teste_2026-09"}
    services.kiwix = fake.client()
    with db() as session:
        page_id = session.execute(
            text("SELECT wiki_page_id FROM document WHERE id = :id"), {"id": PORTUGAL}
        ).scalar_one()
        fake_wiki(services).hits = [WikiHit(page_id, "x", "Portugal", "país")]

        result = search(services, session, principal_id, "capital", k=3)

    assert [(h.document_id, h.level) for h in result.results] == [(PORTUGAL, 2)]
    assert any("unavailable" in w.lower() for w in result.warnings)


def test_sem_wiki_configurado_nada_muda(
    db: sessionmaker[Session], tmp_path: Path, principal_id: int
) -> None:
    zims = make_dir(tmp_path / "z")
    services = make_services(db, zims, default_fake(), FakeEmbedder())
    try:
        assert services.get_wiki() is None
        with db() as session:
            result = search(services, session, principal_id, "capital", k=3)
        assert [h.level for h in result.results] == [1, 1, 1] and result.warnings == []
        assert run_import_scan(services).hot == 0
    finally:
        services.close()


# --- o que se importa ----------------------------------------------------------------------------


def heat_up(services: Services, document_id: str, amount: float) -> None:
    ensure_docs(services, document_id)
    with services.sessions() as session:
        heat.add_document_heat(session, [document_id], amount, 30.0)
        session.commit()


def queued(services: Services) -> dict[str, float]:
    with services.sessions() as session:
        rows = session.execute(
            select(Job.payload, Job.priority).where(
                Job.kind == "import_page", Job.status == "pending"
            )
        ).all()
    return {payload["document_id"]: priority for payload, priority in rows}


def test_varrimento_enfileira_so_os_documentos_quentes(services: Services) -> None:
    heat_up(services, LISBOA, 5.0)
    heat_up(services, PORTUGAL, 1.0)  # abaixo do limiar (3)

    report = run_import_scan(services)

    assert report.hot == 1
    assert list(queued(services)) == [LISBOA]
    assert run_import_scan(services).hot == 0  # já na fila: não se repete


def test_varrimento_expande_um_salto_a_partir_dos_quentes_importados(services: Services) -> None:
    import_now(services, LISBOA)
    heat_up(services, LISBOA, 6.0)
    link(services, LISBOA, BOOKS_REV)  # vizinho de outra fonte: entra
    link(services, LISBOA, REMEDIO, kind="vector", status="proposed")  # proposta: não
    link(services, LISBOA, PORTUGAL, kind="zim_internal")  # interna: não
    link(services, BOOKS_REV, MALICIOSO)  # vizinho do vizinho: não (um salto só)

    report = run_import_scan(services)

    assert report.neighbors == 1
    assert list(queued(services)) == [BOOKS_REV]
    assert queued(services)[BOOKS_REV] == pytest.approx(3.0, abs=0.1)  # metade do calor


def test_varrimento_refaz_as_paginas_cuja_fonte_mudou_de_versao(services: Services) -> None:
    import_now(services, PORTUGAL)
    with services.sessions() as session:
        session.execute(
            text(
                'UPDATE document SET meta = meta || \'{"wiki_version": "2025-01"}\' WHERE id = :i'
            ),
            {"i": PORTUGAL},
        )
        session.commit()

    assert run_import_scan(services).stale == 1
    assert list(queued(services)) == [PORTUGAL]


def test_erro_inesperado_do_wiki_degrada_sem_erro(
    services: Services, principal_id: int, db: sessionmaker[Session]
) -> None:
    fake_wiki(services).boom = True

    with db() as session:
        result = search(services, session, principal_id, "capital", k=3)

    assert [h.level for h in result.results] == [1, 1, 1]
    assert any("Wiki.js unavailable (RuntimeError)" in w for w in result.warnings)


def test_paginas_arquivadas_ou_retiradas_nao_aparecem_no_nivel_2(
    services: Services, principal_id: int, db: sessionmaker[Session]
) -> None:
    import_now(services, PORTUGAL, LISBOA)
    with db() as session:
        ids = dict(session.execute(text("SELECT id, wiki_page_id FROM document")).all())
        fake_wiki(services).hits = [
            WikiHit(ids[PORTUGAL], "p", "Portugal", "país"),
            WikiHit(ids[LISBOA], "l", "Lisboa", "cidade"),
        ]
        session.execute(
            text("UPDATE curation SET archived_at = now() WHERE document_id = :i"), {"i": PORTUGAL}
        )
        session.execute(
            text("UPDATE curation SET decision = 'rejected' WHERE document_id = :i"), {"i": LISBOA}
        )
        session.commit()

        result = search(services, session, principal_id, "capital", k=5)

    assert [h for h in result.results if h.level == 2] == []
