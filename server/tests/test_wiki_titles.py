"""O título da página do Wiki.js (docs/WIKI-IMPORT.md, secção 2.7): o nome do site que se
aprende por fonte, os títulos genéricos e o 1.º cabeçalho do texto."""

from pathlib import Path

import pytest

from ariadne.wiki import boilerplate, convert, titles
from ariadne.wiki.boilerplate import Boilerplate

FIXTURES = Path(__file__).parent / "fixtures" / "wiki"


def page(title: str, h1: str | None = None, og: str | None = None, body: str = "") -> str:
    head = f"<title>{title}</title>"
    if og is not None:
        head += f'<meta property="og:title" content="{og}">'
    heading = f"<h1>{h1}</h1>" if h1 is not None else ""
    return f"<html><head>{head}</head><body>{heading}<p>Texto da página.</p>{body}</body></html>"


def learned(*pages: str) -> Boilerplate:
    return boilerplate.learn(list(pages))


def pick(html: str, bp: Boilerplate, zim_title: str | None = None) -> titles.Chosen:
    root, _ = convert.prepare(html, boilerplate=bp)
    raw = titles.page_titles(html)
    return titles.choose(raw.title if zim_title is None else zim_title, html, bp, root=root)


# --- o nome do site, por prefixo e por sufixo --------------------------------------------------


@pytest.mark.parametrize(
    ("titulos", "bruto", "esperado"),
    [
        # sufixo com « | »
        (
            [
                "A | Internet Encyclopedia of Philosophy",
                "B | Internet Encyclopedia of Philosophy",
                "C | Internet Encyclopedia of Philosophy",
                "D",
            ],
            "Dunyi, Zhou | Internet Encyclopedia of Philosophy",
            "Dunyi, Zhou",
        ),
        # sufixo com « - »
        (
            ["x - MedlinePlus Medical Encyclopedia"] * 3
            + ["y - MedlinePlus Medical Encyclopedia Image"],
            "Thyroid nodule: MedlinePlus Medical Encyclopedia",
            "Thyroid nodule",
        ),
        # prefixo com «[Nome]»
        (
            [f"[OpenWrt Wiki] Página {i}" for i in range(4)],
            "[OpenWrt Wiki] QoS (Network Traffic Control)",
            "QoS (Network Traffic Control)",
        ),
        # prefixo com «: » e maiúsculas diferentes
        (
            ["Low-tech Magazine: Um", "LOW-TECH MAGAZINE: Dois", "Três - LOW-TECH MAGAZINE", "x"],
            "Low-tech Magazine: Insulation",
            "Insulation",
        ),
        # prefixo e sufixo ao mesmo tempo
        (
            [f"Field Listing :: Campo {i} — The World Factbook" for i in range(4)],
            "Field Listing :: Hospital bed density — The World Factbook",
            "Hospital bed density",
        ),
    ],
)
def test_tira_o_nome_do_site_aprendido_na_fonte(
    titulos: list[str], bruto: str, esperado: str
) -> None:
    bp = learned(*(page(t) for t in titulos))

    assert pick(page(bruto), bp).title == esperado


def test_nao_tira_o_que_nao_se_repete_nas_paginas() -> None:
    # «Problema 24: …» tem o mesmo formato mas o número muda: não é o nome do site
    bp = learned(*(page(f"Problema {i}: Algo {i}") for i in range(10, 16)))

    assert not bp.site
    assert pick(page("Problema 24: Permutações lexicográficas"), bp).title == (
        "Problema 24: Permutações lexicográficas"
    )


def test_sem_amostra_o_titulo_fica_como_esta() -> None:
    assert pick(page("Um título - Site"), Boilerplate()).title == "Um título - Site"


# --- títulos que se repetem e o h1/og:title ----------------------------------------------------


def test_titulo_igual_em_muitas_paginas_usa_o_h1() -> None:
    # armypubs: o <title> é o nome do site; o 1.º cabeçalho é o tipo («DA FORM») e o 2.º identifica
    paginas = [
        page("Army Publishing Directorate", h1=h, body=f"<h2>Record Details for {n}</h2>")
        for h, n in (
            ("DA FORM", "DA FORM 199-1"),
            ("TECHNICAL MANUAL", "TM 11-5825"),
            ("DA FORM", "DA FORM 4856"),
        )
    ]
    bp = learned(*paginas)

    assert bp.generic
    chosen = pick(paginas[0], bp)
    assert (chosen.title, chosen.origin) == ("Record Details for DA FORM 199-1", "heading")


def test_o_h1_generico_da_fonte_nao_serve() -> None:
    # lowtech: o <h1> é «LOW-TECH MAGAZINE» em todas as páginas e o og:title é o do artigo
    paginas = [
        page(f"{t} - LOW-TECH MAGAZINE", h1="LOW-TECH MAGAZINE", og=t)
        for t in ("Heat your House", "Solar Boilers", "Wind Power", "Wood Stoves")
    ]
    bp = learned(*paginas)

    assert pick(paginas[0], bp).title == "Heat your House"


def test_h1_que_concorda_com_o_titulo_ganha() -> None:
    html = page(
        "3 Ways to Choose a Food Thermometer - wikiHow Life", h1="How to Choose a Food Thermometer"
    )

    assert pick(html, Boilerplate()).title == "How to Choose a Food Thermometer"


def test_h1_de_outra_coisa_nao_substitui_o_titulo_da_pagina() -> None:
    # wikibooks: o <h1> é o de uma subsecção e o título do ZIM é o caminho da página
    html = page(
        "Science: An Elementary Teacher's Guide/Observing", h1="What are the Process Skills"
    )

    assert pick(html, Boilerplate()).title == "Science: An Elementary Teacher's Guide/Observing"


def test_o_titulo_mais_curto_ganha_quando_o_outro_o_continua() -> None:
    html = page(
        "Adalimumab - NHS",
        h1="Adalimumab - Common brands: Amgevita, Humira, Imraldi",
        og="Adalimumab",
    )
    bp = learned(*(page(f"Medicina {i} - NHS") for i in range(4)))

    assert pick(html, bp).title == "Adalimumab"


def test_limpa_o_pilcrow_e_as_marcas_de_direcao() -> None:
    assert pick(
        page("x", h1="6. Distutils Examples¶"), Boilerplate(), "6. Distutils Examples"
    ).title == ("6. Distutils Examples")
    assert pick(page("‪Massas e Molas‬"), Boilerplate()).title == "Massas e Molas"


# --- o 1.º cabeçalho e as maiúsculas -----------------------------------------------------------


def test_cd3wd_o_titulo_vem_do_primeiro_cabecalho_em_frase() -> None:
    html = (FIXTURES / "cd3wd_real_compost.html").read_text(encoding="utf-8")
    root, _ = convert.prepare(html)

    chosen = titles.choose("PAPEL #7 TÉCNICO", html, Boilerplate(), root=root)

    assert chosen.title == "Understanding composting" and chosen.origin == "heading"
    assert chosen.heading is not None  # o cabeçalho do texto: o importador tira-o do corpo


def test_numero_de_serie_e_titulo_fraco_mesmo_sem_amostra() -> None:
    assert titles.weak("PAPEL #7 TÉCNICO", frozenset())
    assert titles.weak("Main Content", frozenset()) and titles.weak("x", frozenset())
    assert not titles.weak("Problema 24: Permutações", frozenset())


@pytest.mark.parametrize(
    ("antes", "depois"),
    [
        ("UNDERSTANDING COMPOSTING", "Understanding composting"),
        ("OPERAÇÃO AUTOMÓVEL E MANUTENÇÃO", "Operação automóvel e manutenção"),
        ("TM 11-5825-271-34 MANUAL", "TM 11-5825-271-34 manual"),
        ("NASA", "NASA"),  # uma sigla não é uma frase
        ("Frase Normal Com Maiúsculas", "Frase Normal Com Maiúsculas"),
        ("USB", "USB"),
    ],
)
def test_capitalizacao_de_frase_so_para_titulos_em_maiusculas(antes: str, depois: str) -> None:
    assert titles.sentence_case(antes) == depois


def test_o_aprendido_vai_e_volta_em_json() -> None:
    bp = learned(*(page(f"Tema {i} | Site Teste") for i in range(5)))

    assert "site teste" in bp.site
    assert Boilerplate.from_meta(bp.to_meta()) == bp
