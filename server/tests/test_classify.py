"""Classificação à entrada: artigo, curso ou livro (STRUCTURE.md, secção 4)."""

from pathlib import Path

import pytest

from ariadne.sources.classify import (
    ContentClass,
    classify_zim,
    effective_class,
    has_exercises,
    parse_overrides,
)
from ariadne.sources.registry import SourceRegistry
from ariadne.sources.zim import ZimSource
from tests.coursefactory import make_fcc_zim
from tests.zimfactory import make_zim


@pytest.mark.parametrize(
    ("name", "creator", "tags", "expected"),
    [
        ("freecodecamp_en_all", "freeCodeCamp", "FCC;freeCodeCamp", ContentClass.COURSE),
        ("khanacademy_en_all", "", "", ContentClass.COURSE),
        ("wikiversity_en_all", "Wikimedia", "", ContentClass.COURSE),
        ("qualquer_coisa", "", "cursos;pt", ContentClass.COURSE),
        ("wikibooks_en_all", "Wikimedia", "", ContentClass.BOOK),
        ("gutenberg_pt_all", "", "", ContentClass.BOOK),
        ("libretexts_math", "", "", ContentClass.BOOK),
        ("wikipedia_pt_all", "Wikipedia", "", ContentClass.ARTICLE),
        ("stackoverflow.com_en_all", "Stack Exchange", "", ContentClass.ARTICLE),
        ("devdocs_en_python", "DevDocs", "", ContentClass.ARTICLE),
    ],
)
def test_classifica_pelos_metadados(
    name: str, creator: str, tags: str, expected: ContentClass
) -> None:
    result = classify_zim(name=name, creator=creator, tags=tags)
    assert result.content_class is expected
    assert result.reason


def test_um_livro_com_etiqueta_de_curso_continua_livro() -> None:
    assert classify_zim(name="wikibooks_en_all", tags="courses").content_class is ContentClass.BOOK


def test_tipo_de_conteudo_de_cada_classe() -> None:
    assert ContentClass.ARTICLE.content_type == "article"
    assert ContentClass.COURSE.content_type == "lesson"
    assert ContentClass.BOOK.content_type == "chapter"


def test_regra_de_fronteira_com_exercicios_e_curso() -> None:
    reference = "Python tem listas, dicionários e conjuntos. As listas guardam valores."
    with_exercises = "Exercise 1: write a function. Solution: use a loop. Quiz time!"
    assert not has_exercises(reference)
    assert has_exercises(with_exercises)


def test_overrides_manuais_e_por_seccao() -> None:
    overrides = parse_overrides("zim:a=book, zim:b/Python=course ,zim:c=article, lixo")

    assert overrides == {
        "zim:a": ContentClass.BOOK,
        "zim:b/Python": ContentClass.COURSE,
        "zim:c": ContentClass.ARTICLE,
    }
    assert effective_class("zim:a", ContentClass.ARTICLE, overrides) is ContentClass.BOOK
    assert effective_class("zim:b", ContentClass.BOOK, overrides) is ContentClass.BOOK
    assert (
        effective_class("zim:b", ContentClass.BOOK, overrides, "Python/Listas")
        is ContentClass.COURSE
    )
    assert (
        effective_class("zim:b", ContentClass.BOOK, overrides, "Java/Listas") is ContentClass.BOOK
    )
    assert effective_class("zim:x", ContentClass.ARTICLE, overrides) is ContentClass.ARTICLE


def test_override_com_classe_desconhecida() -> None:
    with pytest.raises(ValueError, match="unknown class"):
        parse_overrides("zim:a=romance")


def test_zim_do_freecodecamp_fica_como_curso_e_arquivo(tmp_path: Path) -> None:
    path = make_fcc_zim(tmp_path / "freecodecamp_en_testcurso_2026-08.zim")

    source = ZimSource.open(path)

    assert source.meta["content_class"] == "course"
    assert source.meta["archive_only"] is True
    assert "freeCodeCamp" in source.meta["class_reason"]
    assert source.content_type == "article"  # os documentos do ZIM em si continuam artigos


def test_zim_da_wikipedia_continua_artigo(tmp_path: Path) -> None:
    source = ZimSource.open(make_zim(tmp_path / "wikipedia_pt_teste_2026-09.zim"))

    assert source.meta["content_class"] == "article"
    assert source.meta["archive_only"] is False


def test_o_registo_aplica_as_classificacoes_manuais(tmp_path: Path) -> None:
    make_zim(tmp_path / "wikipedia_pt_teste_2026-09.zim")
    registry = SourceRegistry(
        tmp_path, class_overrides=parse_overrides("zim:wikipedia_pt_teste=book")
    )
    registry.load()
    try:
        meta = registry.get("zim:wikipedia_pt_teste").meta  # type: ignore[union-attr]
        assert meta["content_class"] == "book"
        assert meta["class_reason"] == "SOURCE_CLASS_OVERRIDES"
    finally:
        registry.close()


def test_curso_sem_extrator_continua_pesquisavel(tmp_path: Path) -> None:
    """`course` sem extrator não é arquivo: esconder o que não se sabe extrair seria perdê-lo."""
    make_zim(tmp_path / "wikipedia_pt_teste_2026-09.zim")
    registry = SourceRegistry(
        tmp_path, class_overrides=parse_overrides("zim:wikipedia_pt_teste=course")
    )
    registry.load()
    try:
        source = registry.get("zim:wikipedia_pt_teste")
        assert source is not None
        assert source.meta["content_class"] == "course"
        assert source.meta["extractable"] is False and source.meta["archive_only"] is False
        assert source.archive_only_for("Lisboa") is False
    finally:
        registry.close()


def test_override_por_prefixo_de_caminho_chega_ao_documento(tmp_path: Path) -> None:
    from tests.coursefactory import make_fcc_zim

    make_fcc_zim(tmp_path / "freecodecamp_en_testcurso_2026-08.zim")
    registry = SourceRegistry(
        tmp_path,
        class_overrides=parse_overrides(
            "zim:freecodecamp_en_testcurso=article,zim:freecodecamp_en_testcurso/content=course"
        ),
    )
    registry.load()
    try:
        source = registry.get("zim:freecodecamp_en_testcurso")
        assert source is not None
        assert source.meta["content_class"] == "article"  # a fonte, corrigida à mão
        assert source.meta["archive_only"] is False
        assert source.content_class_of("index/x.html") is ContentClass.ARTICLE
        assert source.content_class_of("content/curriculum/index.json") is ContentClass.COURSE
        assert source.archive_only_for("content/curriculum/index.json") is True  # tem extrator
        assert source.archive_only_for("index/x.html") is False
    finally:
        registry.close()
