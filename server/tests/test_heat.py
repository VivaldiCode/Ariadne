from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from ariadne import heat
from ariadne.db.models import Chunk
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Source as SourceRow

pytestmark = pytest.mark.db

HALF_LIFE = 30.0


@pytest.fixture
def docs(db: sessionmaker[Session]) -> sessionmaker[Session]:
    with db() as s:
        s.add(
            SourceRow(
                id="zim:t", kind="zim", title="T", version="1", wing="t", license_class="open"
            )
        )
        s.flush()
        for name in ("A", "B"):
            s.add(
                DocumentRow(
                    id=f"zim:t/{name}",
                    source_id="zim:t",
                    path=name,
                    version="1",
                    title=name,
                    license_class="open",
                )
            )
        s.flush()
        s.add(Chunk(document_id="zim:t/A", ord=0, text="x"))
        s.commit()
    return db


def age(sessions: sessionmaker[Session], days: float) -> None:
    """Envelhece o último evento de tudo em `days` dias."""
    with sessions() as s:
        past = datetime.now(UTC) - timedelta(days=days)
        s.execute(update(DocumentRow).values(heat_at=past))
        s.execute(update(Chunk).values(heat_at=past))
        s.commit()


def read(sessions: sessionmaker[Session]) -> tuple[float, float]:
    with sessions() as s:
        document = s.scalar(
            select(heat.document_heat(HALF_LIFE)).where(DocumentRow.id == "zim:t/A")
        )
        chunk = s.scalar(select(heat.chunk_heat(HALF_LIFE)).where(Chunk.document_id == "zim:t/A"))
    assert document is not None and chunk is not None
    return document, chunk


def test_formula_em_python() -> None:
    now = datetime.now(UTC)
    assert heat.decayed(8.0, now - timedelta(days=30), now, 30) == pytest.approx(4.0)
    assert heat.decayed(8.0, now - timedelta(days=90), now, 30) == pytest.approx(1.0)
    assert heat.decayed(8.0, now + timedelta(days=1), now, 30) == pytest.approx(8.0)  # sem futuro


def test_eventos_somam_pesos(docs: sessionmaker[Session]) -> None:
    with docs() as s:
        heat.add_document_heat(s, ["zim:t/A"], heat.WEIGHT_SHOWN, HALF_LIFE)
        s.commit()
        heat.add_document_heat(s, ["zim:t/A", "zim:t/A"], heat.WEIGHT_USED, HALF_LIFE)
        s.commit()
        heat.add_document_heat(s, ["zim:t/A"], heat.WEIGHT_FEEDBACK, HALF_LIFE)
        s.commit()
        other = s.scalar(select(DocumentRow.heat).where(DocumentRow.id == "zim:t/B"))
    document, _ = read(docs)
    assert document == pytest.approx(0.3 + 1.0 + 2.0, abs=1e-3)
    assert other == 0


def test_o_calor_decai_com_a_meia_vida_na_leitura(docs: sessionmaker[Session]) -> None:
    with docs() as s:
        heat.add_document_heat(s, ["zim:t/A"], 8.0, HALF_LIFE)
        chunk_id = s.scalars(select(Chunk.id)).one()
        heat.add_chunk_heat(s, [chunk_id], 4.0, HALF_LIFE)
        s.commit()
    age(docs, 30)
    document, chunk = read(docs)
    assert document == pytest.approx(4.0, rel=1e-3)
    assert chunk == pytest.approx(2.0, rel=1e-3)


def test_novo_evento_decai_o_antigo_antes_de_somar(docs: sessionmaker[Session]) -> None:
    with docs() as s:
        heat.add_document_heat(s, ["zim:t/A"], 8.0, HALF_LIFE)
        s.commit()
    age(docs, 60)  # 8 -> 2
    with docs() as s:
        heat.add_document_heat(s, ["zim:t/A"], 1.0, HALF_LIFE)
        s.commit()
        stored = s.scalar(select(DocumentRow.heat).where(DocumentRow.id == "zim:t/A"))
    assert stored == pytest.approx(3.0, rel=1e-3)


def test_compactar_nao_muda_o_que_a_leitura_devolve(docs: sessionmaker[Session]) -> None:
    with docs() as s:
        heat.add_document_heat(s, ["zim:t/A"], 8.0, HALF_LIFE)
        s.commit()
    age(docs, 30)
    before, _ = read(docs)
    with docs() as s:
        touched = heat.compact(s, HALF_LIFE)
        s.commit()
        stored = s.scalar(select(DocumentRow.heat).where(DocumentRow.id == "zim:t/A"))
    assert touched == (1, 0)  # só o documento A tinha calor
    assert stored == pytest.approx(4.0, rel=1e-3)  # agora está gravado
    after, _ = read(docs)
    assert after == pytest.approx(before, rel=1e-3)


def test_compactar_poe_a_zero_o_que_ja_nao_conta(docs: sessionmaker[Session]) -> None:
    with docs() as s:
        heat.add_document_heat(s, ["zim:t/A"], 1.0, HALF_LIFE)
        s.commit()
    age(docs, 30 * 40)  # 40 meias-vidas
    with docs() as s:
        heat.compact(s, HALF_LIFE)
        s.commit()
        assert s.scalar(text("SELECT heat FROM document WHERE id = 'zim:t/A'")) == 0


def test_compactar_em_lotes_e_so_o_que_tem_mais_de_um_dia(docs: sessionmaker[Session]) -> None:
    with docs() as s:
        for n in range(5):
            s.add(
                DocumentRow(
                    id=f"zim:t/D{n}",
                    source_id="zim:t",
                    path=f"D{n}",
                    version="1",
                    title="D",
                    license_class="open",
                )
            )
        s.flush()
        s.execute(
            update(DocumentRow).values(heat=8.0, heat_at=datetime.now(UTC) - timedelta(days=30))
        )
        # Dois documentos mexeram agora mesmo: ficam como estão.
        s.execute(
            update(DocumentRow)
            .where(DocumentRow.id.in_(["zim:t/A", "zim:t/D0"]))
            .values(heat_at=datetime.now(UTC) - timedelta(hours=2))
        )
        s.commit()
        touched = heat.compact(s, HALF_LIFE, batch=2)
        rows = dict(s.execute(select(DocumentRow.id, DocumentRow.heat)).all())
    assert touched == (5, 0)  # 7 documentos com calor, menos os 2 recentes (A e D0)
    assert rows["zim:t/A"] == 8.0 and rows["zim:t/D0"] == 8.0
    assert rows["zim:t/D1"] == pytest.approx(4.0, rel=1e-3)
