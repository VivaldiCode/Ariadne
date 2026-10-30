from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ariadne.db.migrate import upgrade
from ariadne.db.models import Source
from ariadne.db.session import make_engine
from ariadne.sources.registry import SourceRegistry
from tests.zimfactory import make_zim

pytestmark = pytest.mark.db

ZIM = "wikipedia_pt_teste_2026-09.zim"


def _rows(sessions: sessionmaker[Session]) -> dict[str, Source]:
    with sessions() as session:
        return {row.id: row for row in session.scalars(select(Source))}


def test_sync_regista_a_fonte(zim_dir: Path, db: sessionmaker[Session]) -> None:
    registry = SourceRegistry(zim_dir)
    with db() as session:
        report = registry.sync(session)

    assert report.active == ["zim:wikipedia_pt_teste"]
    assert report.errors == {} and report.missing == []
    row = _rows(db)["zim:wikipedia_pt_teste"]
    assert (row.kind, row.status, row.language, row.version) == ("zim", "active", "pt", "2026-09")
    assert row.wing == "wikipedia_pt_teste"
    assert row.path == str(zim_dir / ZIM)
    assert row.meta["kiwix_book"] == "wikipedia_pt_teste_2026-09"
    source = registry.get("zim:wikipedia_pt_teste")
    assert source is not None and source.read("Lisboa").title == "Lisboa"
    assert [s.id for s in registry.active()] == ["zim:wikipedia_pt_teste"]
    registry.close()


def test_sync_sem_mudancas_nao_reabre(zim_dir: Path, db: sessionmaker[Session]) -> None:
    registry = SourceRegistry(zim_dir)
    with db() as session:
        registry.sync(session)
        first = registry.get("zim:wikipedia_pt_teste")
        assert registry.sync_if_changed(session) is None  # a pasta não mudou
        registry.sync(session)
        assert registry.get("zim:wikipedia_pt_teste") is first  # o mesmo ZIM, ainda aberto
        make_zim(zim_dir / "wikipedia_pt_outra_2026-09.zim", name="wikipedia_pt_outra")
        report = registry.sync_if_changed(session)
    assert report is not None
    assert report.active == ["zim:wikipedia_pt_outra", "zim:wikipedia_pt_teste"]
    registry.close()


def test_ficheiro_que_desaparece_fica_missing(zim_dir: Path, db: sessionmaker[Session]) -> None:
    registry = SourceRegistry(zim_dir)
    with db() as session:
        registry.sync(session)
        (zim_dir / ZIM).unlink()
        report = registry.sync(session)

    assert report.active == []
    assert report.missing == ["zim:wikipedia_pt_teste"]
    assert _rows(db)["zim:wikipedia_pt_teste"].status == "missing"
    assert registry.get("zim:wikipedia_pt_teste") is None

    make_zim(zim_dir / ZIM)  # voltou
    with db() as session:
        registry.sync(session)
    assert _rows(db)["zim:wikipedia_pt_teste"].status == "active"
    registry.close()


def test_versao_nova_mantem_o_id(zim_dir: Path, db: sessionmaker[Session]) -> None:
    registry = SourceRegistry(zim_dir)
    with db() as session:
        registry.sync(session)
        (zim_dir / ZIM).unlink()
        make_zim(zim_dir / "wikipedia_pt_teste_2026-10.zim", date="2026-10-01")
        registry.sync(session)

    rows = _rows(db)
    assert list(rows) == ["zim:wikipedia_pt_teste"]  # o mesmo id, sem fonte nova
    assert rows["zim:wikipedia_pt_teste"].version == "2026-10"
    registry.close()


def test_ficheiros_repetidos_e_ilegiveis(zim_dir: Path, db: sessionmaker[Session]) -> None:
    make_zim(zim_dir / "wikipedia_pt_teste_2026-11.zim", date="2026-11-02")  # mesmo Name
    (zim_dir / "estragado_2026-01.zim").write_bytes(b"isto nao e um zim")
    (zim_dir / "notas.txt").write_text("ignorado")
    registry = SourceRegistry(zim_dir)
    with db() as session:
        report = registry.sync(session)

    assert report.active == ["zim:wikipedia_pt_teste"]
    assert report.duplicates == {ZIM: "zim:wikipedia_pt_teste"}  # fica o de 2026-11
    assert list(report.errors) == ["estragado_2026-01.zim"]
    assert _rows(db)["zim:wikipedia_pt_teste"].version == "2026-11"
    registry.close()


def test_pasta_inexistente(tmp_path: Path, db: sessionmaker[Session]) -> None:
    registry = SourceRegistry(tmp_path / "nao-existe")
    with db() as session:
        assert registry.sync(session).active == []


def test_variantes_da_mesma_fonte_convivem(zim_dir: Path, db: sessionmaker[Session]) -> None:
    make_zim(zim_dir / "wikipedia_pt_a_nopic_2026-09.zim", name="wikipedia_pt_a", flavour="nopic")
    make_zim(zim_dir / "wikipedia_pt_a_maxi_2026-09.zim", name="wikipedia_pt_a", flavour="maxi")
    registry = SourceRegistry(zim_dir)
    with db() as session:
        report = registry.sync(session)

    assert report.duplicates == {}
    assert report.active == [
        "zim:wikipedia_pt_a_maxi",
        "zim:wikipedia_pt_a_nopic",
        "zim:wikipedia_pt_teste",
    ]
    registry.close()


def test_falha_na_escrita_nao_deixa_o_registo_sincronizado(
    zim_dir: Path, empty_database_url: str
) -> None:
    # Base sem migrar (o 'serve' antes do 'migrate'): a escrita em `source` falha.
    engine = make_engine(empty_database_url)
    sessions = sessionmaker(engine)
    registry = SourceRegistry(zim_dir)
    with sessions() as session, pytest.raises(ProgrammingError):
        registry.sync(session)
    with sessions() as session, pytest.raises(ProgrammingError):
        registry.sync_if_changed(session)  # não dá a pasta por sincronizada: tenta de novo

    upgrade(empty_database_url)  # a base fica pronta: o pedido seguinte regista tudo
    with sessions() as session:
        report = registry.sync_if_changed(session)
    assert report is not None and report.active == ["zim:wikipedia_pt_teste"]
    assert "zim:wikipedia_pt_teste" in _rows(sessions)
    registry.close()
    engine.dispose()


def test_ficheiros_inacessiveis_nao_partem_o_resto(
    zim_dir: Path, db: sessionmaker[Session]
) -> None:
    (zim_dir / "partido_2026-01.zim").symlink_to(zim_dir / "nao-existe.zim")
    registry = SourceRegistry(zim_dir)
    with db() as session:
        report = registry.sync(session)
        again = registry.sync_if_changed(session)

    assert report.active == ["zim:wikipedia_pt_teste"]
    assert list(report.errors) == ["partido_2026-01.zim"]
    assert again is None
    registry.close()
