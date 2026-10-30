"""O leitor de dumps SQL do MySQL: aspas, escapes, blocos cortados e ficheiros partidos."""

import gzip
import io
from pathlib import Path

import pytest

from ariadne.concepts import dump
from ariadne.concepts.dump import DumpTable, open_dump, parse_tuple, unescape
from tests import sitelinks
from tests.sitelinks import items_dump, items_rows

ITEMS_COLUMNS = ["ips_row_id", "ips_item_id", "ips_site_id", "ips_site_page"]


def read(text: str | bytes, table: str = "wb_items_per_site") -> tuple[DumpTable, list[tuple]]:
    data = text.encode() if isinstance(text, str) else text
    reader = DumpTable(io.BytesIO(data), table)
    return reader, list(reader.rows())


def test_le_um_dump_real_com_colunas_e_linhas() -> None:
    reader, rows = read(items_dump())

    assert reader.columns == ITEMS_COLUMNS
    assert rows == items_rows()
    assert reader.stats.rows == len(rows) and reader.stats.bad_rows == 0


def test_titulo_dificil_aspas_barras_parenteses_e_ponto_e_virgula() -> None:
    _, rows = read(items_dump())

    tricky = next(r for r in rows if r[1] == sitelinks.Q_DIFICIL and r[2] == "ptwiki")
    assert tricky[3] == sitelinks.TRICKY_PT == "O'Neil \\ (teste), parte; 2"


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 64, 1000])
def test_blocos_pequenos_cortam_tuplas_strings_e_utf8_a_meio(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(dump, "CHUNK", chunk)

    reader, rows = read(items_dump())

    assert rows == items_rows()  # incluindo «Révolution des œillets» (UTF-8 de 2 bytes)
    assert reader.columns == ITEMS_COLUMNS
    assert reader.stats.bad_rows == 0


def test_outras_tabelas_e_comentarios_sao_ignorados() -> None:
    text = "INSERT INTO `outra` VALUES (1,'texto; com ''aspas'''),(2,'(3,4)');\n" + items_dump()
    _, rows = read(text)
    assert rows == items_rows()


def test_aspas_duplicadas_null_numeros_e_binary() -> None:
    text = (
        "INSERT INTO `t` VALUES (1,'it''s',NULL,3.5,-2,_binary 'bin\\0ary'),"
        "(2,'',  'a\\nb' , 0x41, 1e3,'\\Z');\n"
    )
    _, rows = read(text, "t")
    assert rows == [
        (1, "it's", None, 3.5, -2, "binary"),
        (2, "", "a\nb", "0x41", 1000.0, "\x1a"),
    ]


def test_sem_create_usa_as_colunas_por_omissao() -> None:
    reader = DumpTable(
        io.BytesIO(b"INSERT INTO `page` VALUES (1,0,'A',0);\n"), "page", ["id", "ns", "t", "r"]
    )
    assert list(reader.rows()) == [(1, 0, "A", 0)]
    assert reader.columns == ["id", "ns", "t", "r"]
    with pytest.raises(dump.DumpError, match="has no column"):
        reader.index("nope")


def test_ficheiro_cortado_a_meio_da_ultima_tupla() -> None:
    text = items_dump()
    cut = text.index("(10,78,'dewiki'") + 12  # a meio da última tupla
    reader, rows = read(text[:cut])

    assert rows == items_rows()[:9]
    assert reader.stats.truncated
    assert reader.stats.bad_rows == 1


def test_tupla_estragada_no_meio_salta_so_essa() -> None:
    text = (
        "INSERT INTO `t` VALUES (1,'a'),(2,'b' 'c' oops),(3,'c'),(4,'d');\n"
        "INSERT INTO `t` VALUES (5,'e');\n"
    )
    reader, rows = read(text, "t")

    assert rows == [(1, "a"), (3, "c"), (4, "d"), (5, "e")]
    assert reader.stats.bad_rows == 1
    assert not reader.stats.truncated


def test_linha_enorme_com_muitas_tuplas() -> None:
    values = ",".join(f"({i},'t''{i}')" for i in range(5000))
    _, rows = read(f"INSERT INTO `t` VALUES {values};\n", "t")

    assert len(rows) == 5000 and rows[4999] == (4999, "t'4999")


def test_open_dump_gz_e_sql(tmp_path: Path) -> None:
    plain = tmp_path / "items.sql"
    plain.write_text(items_dump(), encoding="utf-8")
    packed = sitelinks.write_items_dump(tmp_path / "items.sql.gz")

    for path in (plain, packed):
        with open_dump(path) as stream:
            assert list(DumpTable(stream, "wb_items_per_site").rows()) == items_rows()
    with gzip.open(packed) as handle:
        assert handle.read().startswith(b"-- MySQL dump")


def test_unescape_e_parse_tuple() -> None:
    assert unescape(r"a\'b\\c\"d\%e") == "a'b\\c\"d\\%e"
    assert unescape("sem nada") == "sem nada"
    assert parse_tuple("1,'x',NULL") == (1, "x", None)
    assert parse_tuple("") == ()


def test_gz_cortado_marca_truncado_em_vez_de_rebentar(tmp_path: Path) -> None:
    import os

    rows = [(i, i % 7, "ptwiki", f"Título {i} " + os.urandom(6).hex()) for i in range(3000)]
    packed = gzip.compress(items_dump(rows, per_insert=3000).encode())
    path = tmp_path / "cortado.sql.gz"
    path.write_bytes(packed[: len(packed) // 2])  # o descarregamento parou a meio

    with open_dump(path) as stream:
        table = DumpTable(stream, "wb_items_per_site")
        got = list(table.rows())

    assert table.stats.truncated
    assert 0 < len(got) < len(rows) and got == rows[: len(got)]  # o que se leu está certo
