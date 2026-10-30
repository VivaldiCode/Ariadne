"""Fase 7: a língua original dos conceitos (à mão, por TSV de países) e o CLI `concepts`."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ariadne.cli import app as cli
from ariadne.concepts import origin
from ariadne.config import get_settings
from ariadne.db.models import Concept
from tests import sitelinks
from tests.langzims import PT_CRAVOS, make_lang_dir

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]

QID = f"Q{sitelinks.Q_CRAVOS}"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, database_url: str, tmp_path: Path) -> Iterator[Path]:
    zims = make_lang_dir(tmp_path / "zim")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ZIM_DIR", str(zims))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def concept(db: sessionmaker[Session], qid: str) -> Concept:
    with db() as session:
        return session.scalars(select(Concept).where(Concept.wikidata_qid == qid)).one()


@pytest.fixture
def imported(cli_env: Path) -> Path:
    dump = sitelinks.write_items_dump(cli_env / "items.sql.gz")
    code, output = run("concepts", "import-sitelinks", str(dump))
    assert code == 0, output
    return cli_env


def test_cli_import_sitelinks_mostra_as_contagens(cli_env: Path) -> None:
    dump = sitelinks.write_items_dump(cli_env / "items.sql.gz")

    code, output = run("concepts", "import-sitelinks", str(dump), "--sites", "ptwiki,enwiki")

    assert code == 0, output
    assert "3 concepts with 2+ versions" in output
    assert "3 new concepts" in output
    code, output = run("concepts", "import-sitelinks", str(dump), "--sites", "dewiki")
    assert code == 1 and "No source for the sites: dewiki" in output
    code, output = run("concepts", "import-sitelinks", str(cli_env / "nao-existe.gz"))
    assert code == 2


def test_cli_import_langlinks(cli_env: Path) -> None:
    page = sitelinks.write_items_dump(
        cli_env / "page.sql.gz", sitelinks.page_dump([(1, 0, "Revolução_dos_Cravos", 0)])
    )
    links = sitelinks.write_items_dump(
        cli_env / "ll.sql.gz", sitelinks.langlinks_dump([(1, "en", "Carnation Revolution")])
    )

    code, output = run("concepts", "import-langlinks", str(page), str(links), "--site", "ptwiki")

    assert code == 0, output
    assert "1 concepts with 2+ versions" in output or "1 conceitos" in output
    code, output = run("concepts", "import-langlinks", str(page), str(links), "--site", "xxwiki")
    assert code == 1 and "No source for the site 'xxwiki'" in output


def test_set_origin_a_mao_pelo_cli(imported: Path, db: sessionmaker[Session]) -> None:
    code, output = run("concepts", "set-origin", QID, "pt")
    assert code == 0, output
    assert "original language = pt" in output
    cravos = concept(db, QID)
    assert (cravos.origin_lang, cravos.origin_source) == ("pt", "manual")

    code, _ = run("concepts", "set-origin", QID, "POR")  # ISO 639-3 também serve
    assert code == 0 and concept(db, QID).origin_lang == "pt"
    code, output = run("concepts", "set-origin", QID, "português!")
    assert code == 1 and "Invalid language code" in output
    code, output = run("concepts", "set-origin", "Q999999999", "pt")
    assert code == 1 and "Unknown concept" in output
    code, _ = run("concepts", "set-origin", QID, "--clear")
    assert code == 0 and concept(db, QID).origin_lang is None
    code, _ = run("concepts", "set-origin", QID)
    assert code == 2


def test_import_origins_do_tsv_de_paises(imported: Path, db: sessionmaker[Session]) -> None:
    tsv = imported / "origens.tsv"
    tsv.write_text(
        "qid\tcountry\n"
        f"{QID}\tPT\n"  # Revolução dos Cravos: Portugal
        "http://www.wikidata.org/entity/Q77\tpt\n"  # em URL e minúsculas
        f"Q{sitelinks.Q_DIFICIL}\tCH\n"  # Suíça: plurilingue, fora da tabela
        "Q777\tPT\n"  # conceito que a biblioteca não tem
        "Q1\tPT\tqualquer\n# comentário\n\n"
        "Q2\tXX\n",
        encoding="utf-8",
    )

    code, output = run("concepts", "import-origins", str(tsv))

    assert code == 0, output
    cravos, lisboa = concept(db, QID), concept(db, "Q77")
    assert (cravos.origin_lang, cravos.origin_source) == ("pt", "country:PT")
    assert (lisboa.origin_lang, lisboa.origin_source) == ("pt", "country:PT")
    assert concept(db, f"Q{sitelinks.Q_DIFICIL}").origin_lang is None  # sem dados: não se inventa
    assert "2 concepts with an origin" in output
    assert "2 QIDs outside the library" in output and "2 countries outside the table" in output

    # Repetir não muda nada; e o que foi posto à mão nunca é substituído.
    assert "0 concepts with an origin" in run("concepts", "import-origins", str(tsv))[1]
    run("concepts", "set-origin", QID, "en")
    code, output = run("concepts", "import-origins", str(tsv))
    assert concept(db, QID).origin_lang == "en" and "1 set by hand" in output


def test_tsv_com_paises_em_conflito_fica_sem_origem(
    imported: Path, db: sessionmaker[Session]
) -> None:
    tsv = imported / "conflito.tsv"
    tsv.write_text(f"{QID}\tPT\n{QID}\tES\nQ77\tPT\nQ77\tBR\n", encoding="utf-8")

    code, output = run("concepts", "import-origins", str(tsv))

    assert code == 0, output
    assert concept(db, QID).origin_lang is None  # PT e ES: ambíguo
    assert concept(db, "Q77").origin_lang == "pt"  # PT e BR dão a mesma língua
    assert "1 ambiguous" in output


def test_normalize_lang() -> None:
    assert [origin.normalize_lang(c) for c in ("pt", "PT-br", "por", " en ", "eng", "zh_TW")] == [
        "pt", "pt", "pt", "en", "en", "zh",
    ]  # fmt: skip
    for bad in ("", "p", "portugues", "p1", "123"):
        with pytest.raises(origin.ConceptError):
            origin.normalize_lang(bad)
    assert origin.COUNTRY_LANG["PT"] == origin.COUNTRY_LANG["BR"] == "pt"
    assert "CH" not in origin.COUNTRY_LANG and "BE" not in origin.COUNTRY_LANG


def test_set_origin_enfileira_a_refeitura_das_paginas(
    imported: Path, db: sessionmaker[Session]
) -> None:
    from sqlalchemy import update

    from ariadne.db.models import Document as DocumentRow
    from ariadne.db.models import Job

    with db() as session, session.begin():
        session.execute(
            update(DocumentRow).where(DocumentRow.id == PT_CRAVOS).values(wiki_page_id=3)
        )

    run("concepts", "set-origin", QID, "pt")

    with db() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "import_page")).all()
        assert [j.payload["document_id"] for j in jobs] == [PT_CRAVOS]


def test_cli_dump_cortado_importa_o_que_le_e_sai_com_codigo_3(cli_env: Path) -> None:
    packed = cli_env / "ok.sql.gz"
    sitelinks.write_items_dump(packed)
    data = packed.read_bytes()
    cortado = cli_env / "cortado.sql.gz"
    cortado.write_bytes(data[: len(data) - 12])  # sem o fim do gzip

    code, output = run("concepts", "import-sitelinks", str(cortado))

    assert code == 3, output
    assert "Partial import" in output
