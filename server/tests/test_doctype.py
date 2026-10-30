"""Tipo de documento (docs/WIKI-IMPORT.md, secção 4): artigo, livro ou nada, com a razão.

As fixtures (`tests/fixtures/wiki/doctype/`) são páginas reais da amostra de 164 artigos, cortadas
a menos de 10 KB; o `text` sai de `content_root`, como no importador.
"""

from pathlib import Path

import pytest

from ariadne.sources.zim import content_root
from ariadne.wiki.doctype import BOOK_TEXT, HUGE_TEXT, MIN_TEXT, DocKind, classify

FIXTURES = Path(__file__).parent / "fixtures" / "wiki" / "doctype"


def page(name: str) -> tuple[str, str]:
    html = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
    return html, " ".join(content_root(html).get_text(" ").split())


def classify_fixture(name: str, path: str, source: str, mimetype: str = "text/html") -> DocKind:
    html, text = page(name)
    return classify(path=path, mimetype=mimetype, html=html, text=text, source_name=source)


@pytest.mark.parametrize(
    ("name", "path", "source", "kind"),
    [
        # Wikipédia, StackExchange com pergunta, wikem, nhs, IEP, receitas…: artigos.
        ("wikipedia_pt", "Almagesto", "wikipedia_pt_all", "article"),
        ("wikipedia_en", "Carleigh_DeWald", "wikipedia_en_all", "article"),
        (
            "stackexchange_question",
            "questions/81615/mercedes-benz-battery",
            "mechanics.stackexchange.com_en_all",
            "article",
        ),
        ("wikem_en", "Nickel_toxicity/tr", "wikem_en_all", "article"),
        (
            "wikem_zh",
            "Faculdade_de_Medicina_-_Universidade_de_S%C3%A3o_Paulo/zh",
            "wikem_en_all",
            "article",
        ),
        ("nhs", "www.nhs.uk/medicines/adalimumab/", "nhs.uk_en_medicines", "article"),
        ("iep", "iep.utm.edu/pla-thei/", "internet-encyclopedia-philosophy_en_all", "article"),
        (
            "recipes",
            "publicdomainrecipes.com/ketchup/",
            "publicdomainrecipes.com_en_all",
            "article",
        ),
        ("ifixit_en", "Device/Samsung_Chromebook_XE500C21-A03US", "ifixit_en_all", "article"),
        # O que não é artigo.
        ("stackexchange_user", "users/137545/sugarface", "diy.stackexchange.com_en_all", "skip"),
        ("phet_simulador", "collision-lab_pt.html", "phet_pt_all", "skip"),
        ("libretexts_concha", "index/page_193382", "libretexts.org_en_socialsci", "skip"),
        ("ifixit_vazio", "Device/Toro_Snowthrower_38742_2018", "ifixit_en_all", "skip"),
        ("wikivoyage_desambiguacao", "Hertford_(disambiguation)", "wikivoyage_en_all", "skip"),
        ("iep_anexo", "iep.utm.edu/camus-2/", "internet-encyclopedia-philosophy_en_all", "skip"),
        ("khanacademy_lista", "949c8f437da756659dd02e7353166f6c", "khanacademy_en_all", "skip"),
        (
            "gutenberg_capa",
            "Unterricht%20in%20der%20Beredsamkeit_cover.75632",
            "gutenberg_mul_all",
            "skip",
        ),
        ("gentoo_ja", "Handbook%3ASPARC/Blocks/ProfileChoice/ja", "gentoo_en_all", "skip"),
        (
            "wikisource_pagina",
            "A/Page%3ATrue_stories_of_girl_heroines.djvu/233",
            "wikisource_en_all",
            "skip",
        ),
        # Capítulo de livro do wikisource: vai para o Calibre.
        (
            "wikisource_capitulo",
            "A/Adventures_of_Huckleberry_Finn_(1885)/Chapter_12",
            "wikisource_en_all",
            "book",
        ),
    ],
)
def test_amostra(name: str, path: str, source: str, kind: str) -> None:
    result = classify_fixture(name, path, source)
    assert result.kind == kind, result
    assert result.reason


def test_a_razao_diz_porque() -> None:
    assert "profile" in classify_fixture("stackexchange_user", "users/137545/x", "diy").reason
    assert "redirect" in classify_fixture("libretexts_concha", "index/page_1", "lt").reason
    assert (
        "very little"
        not in classify_fixture("wikipedia_pt", "Almagesto", "wikipedia_pt_all").reason
    )
    assert "links" in classify_fixture("khanacademy_lista", "abc", "khanacademy_en_all").reason
    assert (
        "disambiguation"
        in classify_fixture(
            "wikivoyage_desambiguacao", "Hertford_(disambiguation)", "wikivoyage_en_all"
        ).reason
    )


@pytest.mark.parametrize(
    "path",
    [
        "www.survivorlibrary.com/library/gummeres_surveying_1880.pdf",
        "armypubs.army.mil/epubs/DR_pubs/DR_a/NOCASE-AGO_1952-03-000-WEB-0.pdf",
        "medlineplus.gov/download/genetics/condition/jak3-deficient-severe-combined-immunodeficiency.pdf",
    ],
)
def test_pdf_e_livro(path: str) -> None:
    result = classify(path=path, mimetype="application/pdf", html=None, text="", source_name="x")
    assert result == DocKind("book", "PDF file")


def test_epub_pelo_mimetype_ou_pela_extensao() -> None:
    by_mime = classify(
        path="a", mimetype="application/epub+zip", html=None, text="", source_name="x"
    )
    by_name = classify(path="A/livro.EPUB", mimetype="", html=None, text="", source_name="x")
    assert by_mime.kind == by_name.kind == "book"


def test_multimedia_nao_e_texto() -> None:
    result = classify(path="img/a.png", mimetype="image/png", html=None, text="", source_name="x")
    assert result.kind == "skip"
    assert (
        classify(path="x/y.mp4", mimetype="video/mp4", html=None, text="", source_name="x").kind
        == "skip"
    )


@pytest.mark.parametrize(
    "path",
    [
        "docs.python.org/3.12/genindex-B.html",
        "docs.python.org/3.12/py-modindex.html",
        "Category%3AToshiba_laptops_(AMD64)",
        "A/Category:Medicine",
        "questions/tagged/engine",
        "users/12/ana",
        "A/Wikibooks%3ARequests_for_permissions/Decora",
    ],
)
def test_caminhos_que_nao_sao_artigos(path: str) -> None:
    text = "Um texto bastante longo. " * 40
    assert (
        classify(path=path, mimetype="text/html", html=None, text=text, source_name="x").kind
        == "skip"
    )


def test_namespaces_de_documentacao_sao_conteudo() -> None:
    text = "Key and tag documentation. " * 40
    for path in ("Tag%3Adesignation%3Dcommon", "Key:oneway", "Handbook%3ASPARC/Blocks/Disks"):
        assert (
            classify(path=path, mimetype="text/html", html=None, text=text, source_name="osm").kind
            == "article"
        )


def test_limiar_do_texto_curto() -> None:
    short = "x " * ((MIN_TEXT - 1) // 2)
    long = "palavra " * 60
    assert (
        classify(path="a", mimetype="text/html", html=None, text=short, source_name="x").kind
        == "skip"
    )
    assert (
        classify(path="a", mimetype="text/html", html=None, text=long, source_name="x").kind
        == "article"
    )


def sentences(chars: int, seed: str = "O texto continua com frases normais de um manual. ") -> str:
    return (seed * (chars // len(seed) + 1))[:chars]


def test_texto_longo_com_byline_e_livro() -> None:
    """O manual AUTOMAIN do cd3wd (477 mil caracteres, «Por E. Christopher Cone»)."""
    text = "OPERAÇÃO AUTOMÓVEL E MANUTENÇÃO Por E. Christopher Cone UM manual. " + sentences(
        BOOK_TEXT
    )
    result = classify(
        path="AUTOMAIN.HTM",
        mimetype="text/html",
        html="<p>x</p>",
        text=text,
        source_name="cd3wdproject.org_en_all",
    )
    assert result.kind == "book"
    assert "byline" in result.reason


def test_texto_longo_com_capitulos_em_titulo_e_livro() -> None:
    html = "".join(f"<h2>Chapter {n}</h2><p>texto</p>" for n in range(1, 12))
    text = sentences(BOOK_TEXT + 100)
    result = classify(
        path="livro.html", mimetype="text/html", html=html, text=text, source_name="x"
    )
    assert result.kind == "book"
    assert "chapters" in result.reason


def test_citar_capitulos_no_corpo_nao_faz_um_livro() -> None:
    """Artigos da IEP com 60 mil caracteres citam «Chapter 20» de clássicos: continuam artigos."""
    html = "".join(f"<h2>{n}. Secção</h2><p>Zhuangzi, Chapter {n}</p>" for n in range(1, 15))
    text = "Author Information: Maria Silva. " + sentences(BOOK_TEXT + 100)
    result = classify(
        path="iep.utm.edu/zhou-dun/", mimetype="text/html", html=html, text=text, source_name="iep"
    )
    assert result.kind == "article"


def test_textos_longuissimos_sao_livros_mesmo_sem_pistas() -> None:
    result = classify(
        path="a", mimetype="text/html", html=None, text=sentences(HUGE_TEXT + 10), source_name="x"
    )
    assert result.kind == "book"


def test_fontes_de_livros() -> None:
    text = sentences(5000)
    gutenberg = classify(
        path="Marian%20Storm.39369",
        mimetype="text/html",
        html=None,
        text=text,
        source_name="gutenberg_mul_all",
    )
    assert gutenberg.kind == "book"
    ficha = classify(
        path="Marian%20Storm.39369",
        mimetype="text/html",
        html=None,
        text="Marian Storm",
        source_name="gutenberg_mul_all",
    )
    assert ficha.kind == "skip"


# --- listagens, paginação e arquivo ------------------------------------------------------------

LOWTECH = "lowtechmagazine.com_en_all"


@pytest.mark.parametrize(
    "path",
    [
        "A/www.lowtechmagazine.com/page/2/",
        "A/www.lowtechmagazine.com/cover-story/page/2/",
        "A/www.lowtechmagazine.com/history/page/3/",
        "blog/page-4.html",
        "noticias?page=5",
        "blog/archive/",
        "blog/archives/2020",
        "blog/category/solar/",
        "www.exemplo.pt/categoria/energia",
        "blog/author/maria",
        "www.exemplo.org/blog/2021/05/",
        "www.exemplo.org/blog/2021/05/17/",
    ],
)
def test_paginacao_e_arquivo_sao_skip_pelo_caminho(path: str) -> None:
    html, text = page("wikipedia_pt")  # o conteúdo é de um artigo: só o caminho decide

    result = classify(path=path, mimetype="text/html", html=html, text=text, source_name=LOWTECH)

    assert result.kind == "skip" and "listing" in result.reason


@pytest.mark.parametrize(
    "path",
    [
        "index/page_193382",  # libretexts: o número é o id de uma página de conteúdo
        "A/www.lowtechmagazine.com/2021/12/printed-website-third-volume/",
        "Pagerank",
        "How_to_page/the_team",
        "2021",  # o artigo do ano na Wikipédia
        "blog/2021/05/o-meu-artigo",
    ],
)
def test_caminhos_parecidos_mas_que_sao_artigos(path: str) -> None:
    html, text = page("wikipedia_pt")

    result = classify(path=path, mimetype="text/html", html=html, text=text, source_name="x")

    assert result.kind == "article", path


def test_pagina_de_categoria_de_um_blogue_e_listagem_pelo_conteudo() -> None:
    # lowtech /insulation/: seis «Continue reading» e 40% do texto em ligações
    result = classify_fixture("lowtech_listagem", "A/www.lowtechmagazine.com/insulation/", LOWTECH)

    assert result.kind == "skip"
    assert "read more" in result.reason and "links" in result.reason


def test_um_artigo_com_uma_ligacao_read_more_continua_artigo() -> None:
    html = (
        "<html><body><main><h1>Solar panels</h1>"
        + "<p>"
        + "A solar panel turns sunlight into electricity, and it works best in the open air. " * 30
        + '</p><p><a href="/next">Read more</a> about <a href="/x">cells</a>.</p>'
        "</main></body></html>"
    )
    text = " ".join(content_root(html).get_text(" ").split())

    result = classify(path="solar", mimetype="text/html", html=html, text=text, source_name="x")

    assert result.kind == "article"


def test_fila_de_titulos_com_ligacoes_e_resumos_curtos_e_listagem() -> None:
    items = "".join(
        f'<div><h2><a href="/p{i}">Título número {i} do blogue</a></h2>'
        f"<p>Resumo curto {i} do artigo, com poucas palavras de texto.</p></div>"
        for i in range(8)
    )
    html = f"<html><body><main>{items}</main></body></html>"
    text = " ".join(content_root(html).get_text(" ").split())

    result = classify(path="blog", mimetype="text/html", html=html, text=text, source_name="x")

    assert result.kind == "skip" and "listing of articles" in result.reason
