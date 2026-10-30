"""Fase 7: a página do Wiki.js ganha «Outras línguas», com o selo «fonte original»."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ariadne.concepts import origin
from ariadne.concepts.importers import import_sitelinks
from ariadne.embedder import FakeEmbedder
from ariadne.services import Services
from tests import sitelinks
from tests.fakes import default_fake
from tests.langzims import BOOKS_CRAVOS, EN_CRAVOS, PT_CRAVOS, make_lang_dir
from tests.test_curation import request_import, run_jobs
from tests.test_level3 import make_services
from tests.wikifakes import FakeWiki

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]

QID = f"Q{sitelinks.Q_CRAVOS}"
EN_PAGE = "en/unsorted/carnation-revolution"
PT_PAGE = "pt/por-classificar/revolucao-dos-cravos"
KIWIX_PT = "http://kiwix.publico:8801/content/wikipedia_pt_teste_2026-09/Revolucao_dos_Cravos"


@pytest.fixture
def env(db: sessionmaker[Session], tmp_path: Path) -> Iterator[tuple[Services, FakeWiki]]:
    services = make_services(
        db,
        make_lang_dir(tmp_path / "zim"),
        default_fake(),
        FakeEmbedder(),
        wikijs_url="http://wiki",
        wikijs_token="t",
    )
    wiki = FakeWiki()
    services.wiki = wiki
    dump = sitelinks.write_items_dump(tmp_path / "items.sql.gz")
    import_sitelinks(db, services.registry.active(), dump)
    yield services, wiki
    services.close()


def set_origin(services: Services, lang: str | None) -> None:
    with services.sessions() as session, session.begin():
        origin.set_origin(session, QID, lang)


def section(content: str, heading: str) -> str:
    """O texto de uma secção `### …` do bloco «Ligações»."""
    after = content.split(f"### {heading}\n", 1)[1]
    return after.split("\n###", 1)[0].split("\n---", 1)[0]


def test_outras_linguas_com_o_selo_da_fonte_original(env: tuple[Services, FakeWiki]) -> None:
    services, wiki = env
    set_origin(services, "pt")

    request_import(services, EN_CRAVOS)
    run_jobs(services)
    languages = section(wiki.pages[EN_PAGE].content, "Outras línguas")

    # Os links vão para o kiwix enquanto não há página no Wiki.js.
    assert f"- [Revolução dos Cravos]({KIWIX_PT}) · **fonte original**" in languages
    assert languages.count("fonte original") == 2  # a Wikipédia PT e os Wikilivros PT (ambos pt)
    assert "Mesmo conceito" not in wiki.pages[EN_PAGE].content  # os Wikilivros já estão em línguas


def test_sem_lingua_original_nao_ha_selo(env: tuple[Services, FakeWiki]) -> None:
    services, wiki = env

    request_import(services, EN_CRAVOS)
    run_jobs(services)
    content = wiki.pages[EN_PAGE].content

    assert "### Outras línguas" in content and "fonte original" not in content


def test_a_pagina_da_outra_lingua_passa_a_apontar_para_o_wiki_e_refaz_se_a_origem_muda(
    env: tuple[Services, FakeWiki],
) -> None:
    services, wiki = env
    set_origin(services, "pt")
    request_import(services, EN_CRAVOS, PT_CRAVOS)
    run_jobs(services)
    # A página PT existe: a inglesa refaz-se a apontar para ela (já não para o kiwix).
    run_jobs(services)
    assert f"(/{PT_PAGE}) · **fonte original**" in wiki.pages[EN_PAGE].content
    pt_languages = section(wiki.pages[PT_PAGE].content, "Outras línguas")
    assert f"(/{EN_PAGE})" in pt_languages and "fonte original" not in pt_languages
    # A página da Wikipédia PT mostra os Wikilivros no mesmo idioma como «mesmo conceito».
    assert "### Mesmo conceito" in wiki.pages[PT_PAGE].content
    assert BOOKS_CRAVOS.split("/")[1] in wiki.pages[PT_PAGE].content

    writes = len(wiki.writes)
    set_origin(services, "en")  # a curadoria muda de ideias: o selo muda de página
    run_jobs(services)

    assert len(wiki.writes) > writes
    assert "fonte original" not in section(wiki.pages[EN_PAGE].content, "Outras línguas")
    assert "fonte original" in section(wiki.pages[PT_PAGE].content, "Outras línguas")


def test_bloco_poe_a_fonte_original_primeiro_e_escapa_o_titulo() -> None:
    from ariadne.wiki import render

    block = render.links_block(
        [
            render.LinkEntry("langlink", "Alfa", "en", "Carnation Revolution", "/a/en/x"),
            render.LinkEntry(
                "langlink", "Zeta", "pt", "Revolução [dos] Cravos", "/z/pt/x", None, True
            ),
        ]
    )

    assert block.index("**Zeta (pt)**") < block.index("**Alfa (en)**")
    assert "- [Revolução \\[dos\\] Cravos](/z/pt/x) · **fonte original**" in block
    assert block.count("fonte original") == 1
