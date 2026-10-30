from pathlib import Path

import pytest

from ariadne.sources.base import NotAnArticle, NotFound
from ariadne.sources.zim import ZimSource
from tests.zimfactory import make_zim


@pytest.fixture
def source(zim_dir: Path) -> ZimSource:
    return ZimSource.open(zim_dir / "wikipedia_pt_teste_2026-09.zim")


def test_metadados(source: ZimSource) -> None:
    assert source.id == "zim:wikipedia_pt_teste"
    assert source.kind == "zim"
    assert source.title == "Wikipédia (teste)"
    assert source.language == "pt"  # 'por' no ZIM
    assert source.version == "2026-09"  # Date = 2026-09-15
    assert source.wing == "wikipedia_pt_teste"
    assert source.meta["kiwix_book"] == "wikipedia_pt_teste_2026-09"
    assert source.meta["zim_language"] == "por"
    assert source.article_count() == 4  # 3 artigos e 1 redirect


def test_metadados_em_falta_usam_o_nome_do_ficheiro(tmp_path: Path) -> None:
    path = make_zim(
        tmp_path / "wikipedia_xx_sem-nome_2026-03.zim",
        name=None,
        title=None,
        language="xyz",
        date=None,
    )
    source = ZimSource.open(path)

    assert source.id == "zim:wikipedia_xx_sem-nome"
    assert source.version == "2026-03"
    assert source.language == "xyz"  # sem mapeamento de 2 letras: fica o original
    assert source.title == "wikipedia_xx_sem-nome"


def test_read_limpa_o_html(source: ZimSource) -> None:
    article = source.read("Lisboa")

    assert article.path == "Lisboa"
    assert article.title == "Lisboa"
    assert article.redirected_from is None
    assert "<h1>" in article.html  # o HTML original vem inteiro
    text = article.text
    assert text.startswith("Lisboa é a capital de Portugal e fica junto ao rio Tejo.")
    assert "## História" in text
    assert "### Terramoto" in text
    assert "- Rio Tejo\n- Sete colinas" in text
    lixo = ("Navegação", "Rodapé", "var lixo", "Caixa de navegação", "Referência escondida", "[1]")
    for pedaco in lixo:
        assert pedaco not in text


def test_read_segue_redirects(source: ZimSource) -> None:
    article = source.read("Capital_de_Portugal")

    assert article.path == "Lisboa"
    assert article.redirected_from == "Capital_de_Portugal"
    assert article.title == "Lisboa"


def test_seccoes(source: ZimSource) -> None:
    article = source.read("Lisboa")

    assert [(s.title, s.level) for s in article.sections] == [
        ("Lisboa", 1),  # introdução
        ("História", 2),
        ("Terramoto", 3),
        ("Geografia", 2),
    ]
    historia = article.sections[1]
    assert article.section_text(historia) == (
        "## História\n\nA cidade foi reconstruída depois do terramoto de 1755.\n\n"
        "### Terramoto\n\nO terramoto de 1755 destruiu grande parte da cidade."
    )
    assert article.section_text(article.sections[2]).startswith("### Terramoto")


def test_links_internos(source: ZimSource) -> None:
    # Só o Portugal: sem âncoras repetidas, sem externos, sem o próprio artigo (via redirect),
    # sem os links da navegação nem do rodapé.
    assert source.internal_links("Lisboa") == ["Portugal"]
    assert source.internal_links("Portugal") == ["Lisboa"]
    assert source.internal_links("Outro") == []
    article = source.read("Lisboa")
    assert [(link.path, link.title) for link in article.links] == [("Portugal", "Portugal")]
    assert article.section_links(article.sections[0])[0].path == "Portugal"
    assert article.section_links(article.sections[1]) == []


def test_erros(source: ZimSource) -> None:
    with pytest.raises(NotFound):
        source.read("Nao_existe")
    with pytest.raises(NotAnArticle):
        source.read("_res_/style.css")


def test_iter_paths_sem_redirects_nem_recursos(source: ZimSource) -> None:
    assert sorted(source.iter_paths()) == ["Lisboa", "Outro", "Portugal"]


def test_html_absurdamente_aninhado_nao_rebenta() -> None:
    from ariadne.sources.zim import extract_blocks

    html = "<body>" + "<div>" * 3000 + "texto" + "</div>" * 3000 + "</body>"

    blocks, links = extract_blocks(html, lambda href: None)

    assert [b.text for b in blocks] == ["texto"] and links == []


def test_redirect_traz_o_titulo_alternativo(source: ZimSource) -> None:
    article = source.read("Capital_de_Portugal")

    assert (article.path, article.redirected_from) == ("Lisboa", "Capital_de_Portugal")
    assert article.redirected_title == "Capital de Portugal"
    assert source.read("Lisboa").redirected_title is None


def test_redirects_lista_os_titulos_alternativos(source: ZimSource) -> None:
    assert list(source.redirects()) == [("Capital de Portugal", "Lisboa")]


def test_title_key_normaliza_para_comparar_titulos() -> None:
    from ariadne.textutil import title_key

    assert title_key("  Revolução_dos   Cravos. ") == "revolução dos cravos"
    assert title_key("REVOLUÇÃO DOS CRAVOS") == title_key("Revolução dos Cravos!")
    assert title_key("Revolucao dos Cravos") != title_key("Revolução dos Cravos")  # acentos contam
    assert title_key("Straße") == title_key("STRASSE")  # casefold
    assert title_key(" ._ ") == ""


def test_projeto_e_site_do_wikidata(source: ZimSource) -> None:
    assert source.meta["project"] == "wikipedia"
    assert source.meta["wiki_language"] == "pt"
    assert source.meta["site"] == "ptwiki"


@pytest.mark.parametrize(
    ("zim_name", "language", "site"),
    [
        ("wikipedia_pt_all", "pt", "ptwiki"),
        ("wikiquote_en_maxi", "en", "enwikiquote"),
        ("wikibooks_es", None, "eswikibooks"),
        ("wiktionary_all", "fr", "frwiktionary"),
        ("wikipedia_en_ray-charles", "en", "enwiki"),
        ("stackoverflow.com_en_all", "en", None),
        ("mdwiki_en_all", "en", None),
        ("wikipedia_all", None, None),
        ("wikipedia_en_simple_all", "en", "simplewiki"),
        ("wiktionary_en_simple", "en", "simplewiktionary"),
        ("wikipedia_zh-classical_all", "zh", "zh_classicalwiki"),
        ("wikipedia_zh-min-nan_all", "zh", "zh_min_nanwiki"),
        ("wikipedia_en_classical", "en", None),  # variante sem site próprio conhecido: fora
    ],
)
def test_identify_projeto(zim_name: str, language: str | None, site: str | None) -> None:
    from ariadne.sources.projects import identify

    found = identify(zim_name, language)
    assert found.get("site") == site
    if site:
        assert found["project"] == zim_name.split("_")[0]


def test_find_by_title_nao_le_o_conteudo(source: ZimSource) -> None:
    assert source.find_by_title("Lisboa") == ("Lisboa", "Lisboa")
    # Pelo título do redirect: segue-o até ao artigo.
    assert source.find_by_title("Capital de Portugal") == ("Lisboa", "Lisboa")
    assert source.find_by_title("Capital_de_Portugal") == ("Lisboa", "Lisboa")
    assert source.find_by_title("Não existe") is None
    assert source.find_by_title("_res_/style.css") is None  # não é HTML


def test_find_by_title_procura_primeiro_pelo_caminho_canonico(tmp_path: Path) -> None:
    articles = {
        "Foo": ("Bar", "<p>Um artigo cujo título de apresentação é Bar.</p>"),
        "Bar": ("Foo", "<p>Um artigo cujo caminho é Bar e o título Foo.</p>"),
    }
    zim = ZimSource.open(
        make_zim(tmp_path / "wikipedia_pt_x_2026-09.zim", articles=articles, redirects={})
    )

    assert zim.find_by_title("Bar") == ("Bar", "Foo")  # o caminho «Bar», não o título «Bar»
    assert zim.find_by_title("Foo") == ("Foo", "Bar")


def test_muitos_zim_nao_esgotam_os_ficheiros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Com mais ZIM do que ZIM_MAX_OPEN, os que saem do conjunto reabrem-se na leitura seguinte."""
    from ariadne.sources import zim

    monkeypatch.setattr(zim, "_MAX_OPEN", 2)
    sources = [
        ZimSource.open(
            make_zim(tmp_path / f"wikipedia_pt_t{i}_2026-09.zim", name=f"wikipedia_pt_t{i}")
        )
        for i in range(5)
    ]
    assert len(zim._open_archives) <= 2
    for source in sources:  # todos continuam legíveis
        assert source.read("Lisboa").title
    assert len(zim._open_archives) <= 2


def test_erro_inesperado_do_libzim_fica_no_zim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Há ZIM reais cujo erro do libzim nem é UTF-8 (UnicodeDecodeError): vira SourceError."""
    from ariadne.sources import zim
    from ariadne.sources.base import SourceError

    def broken(path: object) -> None:
        raise UnicodeDecodeError("utf-8", b"\x8a", 0, 1, "invalid start byte")

    monkeypatch.setattr(zim, "Archive", broken)
    with pytest.raises(SourceError, match="UnicodeDecodeError"):
        ZimSource.open(tmp_path / "estragado_2026-01.zim")
