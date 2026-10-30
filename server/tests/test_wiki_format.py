"""Regras de formatação do Markdown do Wiki.js (WIKI-IMPORT.md, secções 2 e 3).

As fixtures de `tests/fixtures/wiki/` são troços (menos de 15 KB) de páginas reais da amostra de 41
ZIM: o texto do cd3wd (só `<br>`), a Wikipédia, o wikihow e uma tabela.
"""

import json
import re
from pathlib import Path

import pytest

from ariadne.config import Settings
from ariadne.curation import flags
from ariadne.sources.zim import ZimSource
from ariadne.wiki import boilerplate, convert
from ariadne.wiki import render as pagerender
from ariadne.wiki.boilerplate import Boilerplate
from tests.zimfactory import make_zim

FIXTURES = Path(__file__).parent / "fixtures" / "wiki"
CD3WD = [
    "cd3wd_automain.html",
    "cd3wd_irma_grdnsilg.html",
    "cd3wd_irma_compost.html",
    "cd3wd_irma_x5417.html",
    "cd3wd_irma_default.html",
]


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def render(
    html: str, *, images: dict[str, str] | None = None, bp: Boilerplate | None = None
) -> str:
    root, _ = convert.prepare(html, boilerplate=bp)
    return convert.to_markdown(root, {}, images)


def paragraphs(markdown: str) -> list[str]:
    return [p for p in markdown.split("\n\n") if p.strip()]


# --- cd3wd: texto só com <br> ----------------------------------------------------------------


@pytest.fixture(scope="module")
def cd3wd_boilerplate() -> Boilerplate:
    return boilerplate.learn([load(name) for name in CD3WD])


def test_cd3wd_aprende_a_navegacao_da_fonte(cd3wd_boilerplate: Boilerplate) -> None:
    assert "back to home page of cd3wd project" in cd3wd_boilerplate.lines
    assert (
        "home - english - french - german - italian - portuguese - spanish"
        in cd3wd_boilerplate.lines
    )


def test_cd3wd_sem_boilerplate_a_barra_e_o_logotipo_saem_pelas_regras_do_topo() -> None:
    # nenhuma amostra da fonte: o topo (logótipo, «Back to Home…», barra de línguas) e o «Donate»
    # do fim saem pela estrutura, não por terem sido vistos noutras páginas
    for name in ("cd3wd_real_compost.html", "cd3wd_real_grdnsilg.html", "cd3wd_automain.html"):
        root, _ = convert.prepare(load(name))
        markdown = convert.to_markdown(root, {}, {})
        assert "Back to" not in markdown and "Home" not in markdown, name
        assert "English" not in markdown and "Portuguese" not in markdown, name
        srcs = [r.src for r in convert.images(root)]
        assert not [s for s in srcs if "logo" in s.lower() or "gofundme" in s.lower()], name


def test_cd3wd_a_descricao_nunca_e_a_barra_de_navegacao() -> None:
    for name in ("cd3wd_real_compost.html", "cd3wd_real_grdnsilg.html"):
        markdown = render(load(name))
        description = pagerender.summary(markdown)
        assert len(description) >= 80 and "English" not in description, name
        assert "Home" not in description and "&amp;" not in description, name
    compost = pagerender.summary(render(load("cd3wd_real_compost.html")))
    assert compost.startswith("Este papel é um de uma série")  # e não o «Por J. Walter Fitts»


@pytest.mark.parametrize(
    "markdown",
    [
        "[Home](a) - [English](b) - [French](c) - [German](d) - [Italian](e) - [Spanish](f)",
        "Home - English - French - German - Italian - Portuguese - Spanish",
        "Home | English | French | German | Italian | Portuguese | Spanish",
        "[Voltar ao início](a) • [Lista de publicações](b) • [Contacto](c)",
    ],
)
def test_a_descricao_salta_as_barras_de_navegacao(markdown: str) -> None:
    texto = "Um parágrafo com texto corrido, frases completas e comprimento suficiente para servir."

    assert pagerender.is_navigation(markdown)
    assert pagerender.summary(f"{markdown}\n\n{texto}") == texto
    assert pagerender.summary(markdown) == ""  # e sem mais nada, não há descrição


def test_a_descricao_aceita_uma_definicao_numerada_se_nao_ha_mais_nada() -> None:
    # wiktionary: a página é uma lista de definições; sem parágrafos, serve a 1.ª
    markdown = "1. terceira pessoa do singular do futuro do presente do verbo enegrecer"

    assert pagerender.summary(markdown).startswith("1. terceira pessoa do singular")


def test_a_descricao_nao_toma_texto_normal_por_navegacao() -> None:
    assert not pagerender.is_navigation("A rota vai de Lisboa ao Porto, passando por Coimbra.")
    assert not pagerender.is_navigation(
        "Ver [Lisboa](a) e o seu [Tejo](b) para saber mais sobre isto."
    )


def test_cd3wd_refluxo_paragrafos_titulos_e_navegacao(cd3wd_boilerplate: Boilerplate) -> None:
    markdown = render(load("cd3wd_automain.html"), bp=cd3wd_boilerplate)

    assert "Back to Home" not in markdown and "Home - English" not in markdown
    assert not markdown.startswith("---")  # a linha que separava o menu também sai
    # títulos: linhas em MAIÚSCULAS (juntas, quando partidas em duas) e numeradas
    assert "## OPERAÇÃO AUTOMÓVEL E MANUTENÇÃO" in markdown
    assert "## DEDICAÇÃO DE" in markdown
    assert "### 2.02 Loading o Veículo" in markdown
    # parágrafos: o `<br>` simples junta as linhas, a linha em branco separa
    assert "Por E. Christopher Cone" in paragraphs(markdown)
    assert any(
        p.startswith("Todos os direitos reservados. Nenhuma parte desta publicação")
        and p.endswith("sem permissão escrita do publicador.")
        and "\n" not in p
        for p in paragraphs(markdown)
    )
    assert not re.search(r"\S {2,}\S", markdown)  # sem os espaços do recuo do original
    # os passos numerados continuam um por parágrafo
    assert "1. Leave alguns polegadas (cm) de fio" in markdown
    assert "2. Pull a parte principal do fio" in markdown
    assert not convert.format_quality(markdown).needs_rescue


def test_cd3wd_imagens_do_word_sem_a_linha_do_nome_do_ficheiro(
    cd3wd_boilerplate: Boilerplate,
) -> None:
    html = load("cd3wd_automain.html")
    root, _ = convert.prepare(html, boilerplate=cd3wd_boilerplate)
    refs = convert.images(root)
    assert [r.src for r in refs] == ["../GIF/AOM1.GIF", "../GIF/AOM19.GIF"]  # o logótipo saiu

    markdown = convert.to_markdown(root, {}, {"../GIF/AOM1.GIF": "/ariadne/ab/um.gif"})

    assert markdown.count("![") == 1 and "![](/ariadne/ab/um.gif)" in markdown
    assert "aom1.gif (600x600)" not in markdown and "aom19.gif" not in markdown


def test_refluxo_junta_palavras_hifenizadas_e_refaz_listas() -> None:
    linhas = [
        "CAPÍTULO 3",
        "",
        "A manu-",
        "tenção do motor é feita todos os meses, com cuidado.",
        "Segunda frase do mesmo parágrafo, sempre na mesma linha.",
        "",
        "Fazer o seguinte:",
        "- verificar o óleo e o nível do líquido de refrigeração",
        "• apertar as correias",
        "   com uma chave de 12 mm",
        "o limpar o filtro",
        "",
        "1) abrir o capô",
        "2) tirar a vareta",
        "",
        "Norte-",
        "Americano fica com hífen, porque a palavra seguinte começa com maiúscula.",
    ]
    html = "<body>" + "<br>\n".join(linhas) + "</body>"

    markdown = render(html)

    assert "## CAPÍTULO 3" in markdown
    assert "A manutenção do motor é feita todos os meses, com cuidado. Segunda frase" in markdown
    assert (
        "Fazer o seguinte:\n\n- verificar o óleo e o nível do líquido de refrigeração" in markdown
    )
    assert "- apertar as correias com uma chave de 12 mm\n  - limpar o filtro" in markdown
    assert "1. abrir o capô\n2. tirar a vareta" in markdown
    assert "Norte- Americano" in markdown


def test_refluxo_nao_mexe_em_poucas_quebras_nem_em_listas_de_links() -> None:
    verso = "<p>Rosas são vermelhas<br>violetas são azuis<br>o açúcar é doce</p>"
    assert "Rosas são vermelhas  \nvioletas são azuis" in render(verso)

    menu = "<div>" + "<br>".join(f"<a href='x{i}'>Item {i}</a>" for i in range(10)) + "</div>"
    markdown = render(menu)
    assert markdown.count("\n") >= 9  # continua uma linha por item, não um parágrafo corrido


def test_refluxo_ignora_o_codigo() -> None:
    codigo = "<pre>" + "linha<br>" * 10 + "</pre>"

    assert render(codigo).count("linha") == 10


def test_indice_de_titulos_numerados_vira_lista() -> None:
    linhas = ["INTRODUÇÃO", ""]
    for numero in range(1, 6):
        linhas += [f"{numero}.0 Assunto numero {numero}", ""]
    html = "<body>" + "<br>\n".join(linhas) + "</body>"

    markdown = render(html)

    assert "- 1.0 Assunto numero 1\n- 2.0 Assunto numero 2" in markdown
    assert "###" not in markdown


# --- Wikipédia -------------------------------------------------------------------------------


def test_wikipedia_almagesto_legenda_imagem_e_limpeza() -> None:
    html = load("wikipedia_pt_almagesto.html")
    root, _ = convert.prepare(html)
    refs = convert.images(root)
    assert len(refs) == 1
    assert refs[0].src.endswith("/Almagest_1.jpeg")
    assert refs[0].caption == "Figura de uma tradução do Almagesto em latim, de 1451."

    markdown = convert.to_markdown(root, {}, {refs[0].src: "/ariadne/0c/abc.jpg"})

    assert markdown.startswith("![](/ariadne/0c/abc.jpg)\n\n*Figura de uma tradução do Almagesto")
    assert markdown.count("Figura de uma tradução") == 1  # a legenda não fica duplicada
    assert "## Ver também" in markdown
    assert "Referências" not in markdown  # o título vazio saiu
    assert "emitido por" not in markdown and "licenciado" not in markdown
    assert "Strabo" not in markdown and "Commons" not in markdown  # ícones e galeria sem envio


def test_sem_envio_a_imagem_sai_e_a_legenda_fica_uma_vez() -> None:
    markdown = render(load("wikipedia_pt_almagesto.html"))

    assert "![" not in markdown
    assert markdown.count("Figura de uma tradução do Almagesto em latim") == 1


def test_a_linha_de_licenca_do_zim_sai_nas_duas_linguas() -> None:
    en = "<p>Texto do artigo.</p><div><p>This article is issued from Wikipedia. CC BY-SA.</p></div>"
    pt = "<p>Texto.</p><p>Este artigo é emitido por Wikipédia. O texto é licenciado sob CC.</p>"
    footer = '<h2>Notas</h2><div class="zim-footer">Qualquer coisa noutra língua</div>'

    assert render(en) == "Texto do artigo."
    assert render(pt) == "Texto."
    assert render("<p>Texto.</p>" + footer) == "Texto."


def test_titulos_vazios_saem_mas_os_com_conteudo_ficam() -> None:
    html = (
        "<h2>Introdução</h2><p>Texto.</p>"
        "<h2>Ver também</h2><h3>Subtítulo vazio</h3>"
        "<h2>Notas</h2><ul><li>uma nota</li></ul>"
        "<h2>Referências</h2>"
    )

    assert render(html) == "## Introdução\n\nTexto.\n\n## Notas\n\n- uma nota"


def test_um_titulo_so_com_subtitulo_com_conteudo_fica() -> None:
    html = "<h2>Geral</h2><h3>Detalhe</h3><p>Texto.</p>"

    assert render(html) == "## Geral\n\n### Detalhe\n\nTexto."


# --- wikihow ---------------------------------------------------------------------------------


def test_wikihow_imagens_nos_passos() -> None:
    html = load("wikihow_termometro.html")
    root, _ = convert.prepare(html)
    refs = convert.images(root)
    assert len(refs) == 3
    assert refs[0].alt == "Image titled Choose a Food Thermometer Step 1"
    urls = {r.src: f"/ariadne/{i:02x}/img{i}.webp" for i, r in enumerate(refs)}

    markdown = convert.to_markdown(root, {}, urls)

    assert "1. ![Image titled Choose a Food Thermometer Step 1](/ariadne/00/img0.webp)" in markdown
    assert "2. ![Image titled Choose a Food Thermometer Step 2](/ariadne/01/img1.webp)" in markdown
    assert markdown.count("![") == 3
    assert "**Choose a digital thermometer.**" in markdown
    assert "smallUrl" not in markdown  # o JSON escondido do wikihow não passa


def test_imagens_que_nao_foram_enviadas_saem_do_markdown() -> None:
    html = load("wikihow_termometro.html")
    root, _ = convert.prepare(html)
    refs = convert.images(root)

    markdown = convert.to_markdown(root, {}, {refs[1].src: "/ariadne/01/img1.webp"})

    assert markdown.count("![") == 1 and "Step 2" in markdown


# --- tabela ----------------------------------------------------------------------------------


def test_tabela_gfm_e_caixa_de_informacao_em_lista() -> None:
    markdown = render(load("wikipedia_en_tabela.html"))

    assert (
        "| Virtual channel | Digital channel | Picture resolution | Content |\n"
        "| --- | --- | --- | --- |\n"
        "| 4.1 | 35 UHF | 1080i | TV Jornal / SBT |"
    ) in markdown
    assert "- **Affiliations**: SBT" in markdown
    assert "## Digital signal" in markdown
    assert "## References" not in markdown


# --- legendas e imagens ----------------------------------------------------------------------


def test_legendas_de_figcaption_thumbcaption_e_gallerytext() -> None:
    html = """
    <figure><img src="a.png" alt="A"><figcaption>Legenda <b>A</b>.</figcaption></figure>
    <div class="thumb"><div class="thumbinner"><a href="File:b.png"><img src="b.png"></a>
      <div class="thumbcaption"><div class="magnify"><a><img src="magnify.png"></a></div>
      Legenda B</div></div></div>
    <ul><li class="gallerybox"><div class="thumb"><img src="c.png"></div>
      <div class="gallerytext">Legenda C</div></li></ul>
    <p><img src="d.png"> sem legenda; <img src="a.png"> repetida</p>
    """
    root, _ = convert.prepare(html)

    refs = convert.images(root)

    assert refs == [
        convert.ImageRef("a.png", "A", "Legenda A."),
        convert.ImageRef("b.png", "", "Legenda B"),
        convert.ImageRef("c.png", "", "Legenda C"),
        convert.ImageRef("d.png", "", ""),
    ]
    markdown = convert.to_markdown(root, {}, {r.src: f"/u/{r.src}" for r in refs})
    assert "![A](/u/a.png)\n\n*Legenda **A**.*" in markdown
    assert "![](/u/b.png)\n\n*Legenda B*" in markdown
    assert "![](/u/c.png)" in markdown and "*Legenda C*" in markdown
    assert "magnify" not in markdown
    assert markdown.count("Legenda") == 3  # cada legenda uma vez


def test_imagem_carregada_a_preguica_usa_o_data_src() -> None:
    html = '<p><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" data-src="real.jpg"></p>'
    root, _ = convert.prepare(html)

    assert [r.src for r in convert.images(root)] == ["real.jpg"]


def test_imagem_no_meio_do_texto_fica_no_texto() -> None:
    root, _ = convert.prepare('<p>A fórmula <img src="f.png" alt="x"> vale.</p>')

    assert convert.to_markdown(root, {}, {"f.png": "/u/f.png"}) == "A fórmula ![x](/u/f.png) vale."


# --- avisos e código -------------------------------------------------------------------------


def test_avisos_viram_citacao_com_classe_do_wikijs() -> None:
    html = (
        '<div class="admonition note"><p class="admonition-title">Nota</p>'
        "<p>Isto importa.</p></div>"
        '<div class="admonition warning"><p class="admonition-title">Cuidado</p>'
        "<p>Isto parte.</p></div>"
        '<p class="note">Uma nota solta.</p>'
        '<div class="tip">Dica.</div>'
        '<table><tr><td class="note">célula</td></tr></table>'
    )

    markdown = render(html)

    assert "> **Nota**\n>\n> Isto importa.\n{.is-info}" in markdown
    assert "> **Cuidado**\n>\n> Isto parte.\n{.is-warning}" in markdown
    assert "> Uma nota solta.\n{.is-info}" in markdown
    assert "> Dica.\n{.is-success}" in markdown
    assert markdown.count("{.is-") == 4  # a célula de tabela não vira citação


@pytest.mark.parametrize(
    ("html", "linguagem"),
    [
        (
            '<div class="highlight-python3 notranslate"><div class="highlight">'
            "<pre>x = 1</pre></div></div>",
            "python3",
        ),
        ('<pre><code class="language-js">let x = 1</code></pre>', "js"),
        ('<pre class="brush: bash">ls -l</pre>', "bash"),
        ('<pre class="lang-c prettyprint">int x;</pre>', "c"),
        ('<pre data-lang="Rust">fn main() {}</pre>', "rust"),
        ('<div class="highlight-default notranslate"><pre>>>> 1</pre></div>', ""),
        ("<pre>texto simples</pre>", ""),
    ],
)
def test_codigo_leva_a_lingua_quando_a_classe_a_indica(html: str, linguagem: str) -> None:
    markdown = render(html)

    assert markdown.startswith(f"```{linguagem}\n") and markdown.endswith("\n```")


# --- a forma que ficou -----------------------------------------------------------------------


def test_format_quality_marca_o_texto_sem_estrutura() -> None:
    sem_estrutura = render(
        "<p>" + "Uma frase comprida sem pontuação nenhuma e sem fim " * 120 + "</p>"
    )

    verdict = convert.format_quality(sem_estrutura)

    assert len(sem_estrutura) > 3000
    assert verdict.needs_rescue and "no headings" in verdict.reason


def test_format_quality_aceita_o_que_tem_forma() -> None:
    texto = "Uma frase comprida sem pontuação nenhuma e sem fim " * 120
    assert not convert.format_quality(render(f"<h2>Título</h2><p>{texto}</p>")).needs_rescue
    assert not convert.format_quality(render("<p>curto e sem títulos</p>")).needs_rescue
    muitos = "".join(f"<p>{texto[:400]}</p>" for _ in range(12))  # parágrafos médios de 400
    assert not convert.format_quality(render(muitos)).needs_rescue
    assert convert.format_quality("").reason == ""


def test_format_quality_ignora_codigo_e_tabelas() -> None:
    codigo = "```\n" + "x = 1\n" * 1000 + "```"
    tabela = "\n".join("| a | b |" for _ in range(300))

    assert not convert.format_quality(codigo).needs_rescue
    assert not convert.format_quality(tabela).needs_rescue


# --- boilerplate -----------------------------------------------------------------------------


def _pagina(i: int, menu: bool = True) -> str:
    nav = "<div>Voltar à página inicial do projeto</div><img src='../../logo.PNG'>" if menu else ""
    return (
        f"<body>{nav}<h2>Referências</h2><p>Texto único da página número {i}, só aqui.</p></body>"
    )


def test_learn_com_poucas_paginas_devolve_vazio() -> None:
    assert not boilerplate.learn([_pagina(i) for i in range(4)])


def test_learn_so_guarda_o_que_passa_do_limiar() -> None:
    paginas = [_pagina(i) for i in range(6)] + [_pagina(i, menu=False) for i in range(6, 14)]
    # o menu está em 6 de 14 (43%): passa os 30%; o texto único e os títulos não entram
    learned = boilerplate.learn(paginas)

    assert learned.lines == frozenset({"voltar à página inicial do projeto"})
    assert learned.images == frozenset({"logo.png"})  # «../../» e maiúsculas não contam
    assert not boilerplate.learn(paginas, threshold=0.5)


def test_learn_ignora_o_que_so_aparece_em_duas_paginas() -> None:
    paginas = [_pagina(i, menu=i < 2) for i in range(8)]

    assert not boilerplate.learn(paginas, threshold=0.1)


def test_boilerplate_vai_e_volta_em_json() -> None:
    learned = boilerplate.learn([_pagina(i) for i in range(6)])

    again = Boilerplate.from_meta(json.loads(json.dumps(learned.to_meta())))

    assert again == learned
    assert boilerplate.from_meta(learned.to_meta()) == learned
    assert boilerplate.to_meta(learned) == learned.to_meta()
    assert not Boilerplate.from_meta(None) and not Boilerplate.from_meta({})


def test_prepare_tira_as_linhas_do_boilerplate_e_o_que_ficou_vazio() -> None:
    learned = Boilerplate(frozenset({"voltar à página inicial do projeto"}), frozenset())
    html = "<ul><li>Voltar à  PÁGINA inicial do projeto</li></ul><p>Conteúdo.</p>"

    assert render(html, bp=learned) == "Conteúdo."


def _com_barra(i: int, barra: str) -> str:
    return f"<body>{barra}<h2>Secção</h2><p>Texto único da página número {i}, só aqui.</p></body>"


BARRAS = [
    '<p><a href="/">Início</a> - <a href="/en">English</a> - <a href="/fr">French</a></p>',
    '<p><a href="/">Início</a> | <a href="/en">English</a> | <a href="/fr">French</a></p>',
    '<div><a href="/">Início</a> • <a href="/en">English</a> • <a href="/fr">French</a></div>',
    '<ul><li><a href="/">Início</a></li><li><a href="/en">English</a></li>'
    '<li><a href="/fr">French</a></li></ul>',
]


def test_learn_compara_as_barras_de_ligacoes_sem_separadores() -> None:
    # a barra está em 3 de 8 páginas, cada vez com outros separadores: é a mesma barra
    paginas = [_com_barra(i, BARRAS[i % 3]) for i in range(3)]
    paginas += [_com_barra(i, "") for i in range(3, 8)]
    learned = boilerplate.learn(paginas)

    assert "início english french" in learned.lines  # a chave não leva separadores
    nova = _com_barra(99, BARRAS[3])  # o mesmo menu como lista de ligações (um bloco)
    assert "Início" in render(nova)  # sem a amostra, uma lista de ligações não se tira
    assert "Início" not in render(nova, bp=learned) and "English" not in render(nova, bp=learned)


def test_learn_aprende_barras_de_ligacoes_que_aparecem_em_poucas_paginas() -> None:
    # 3 de 20 páginas (15%) abaixo dos 30%: as linhas normais não passariam, as de ligações sim
    paginas = [_com_barra(i, BARRAS[0]) for i in range(3)]
    paginas += [_com_barra(i, "") for i in range(3, 20)]

    learned = boilerplate.learn(paginas)

    assert "início english french" in learned.lines
    assert not boilerplate.learn(paginas[:2] + paginas[3:8]).lines  # só em 2 páginas: não


def test_a_amostra_cresce_nas_fontes_pequenas() -> None:
    assert boilerplate.sample_size(30) == 30  # lê-se tudo o que há
    assert boilerplate.sample_size(5_000) == boilerplate.SAMPLE_SMALL
    assert boilerplate.sample_size(2_000_000) == boilerplate.SAMPLE_BIG
    assert boilerplate.SAMPLE_SMALL > boilerplate.SAMPLE_BIG > 40


def test_learn_aprende_o_molde_do_texto_escondido_com_tres_paginas() -> None:
    wikihow = load("wikihow_termometro.html")

    learned = boilerplate.learn([wikihow] * 3)

    assert learned.hidden == frozenset({"x this article was co-authored"})
    assert not learned.lines and not learned.images  # com menos de 5 páginas só os moldes
    assert Boilerplate.from_meta(learned.to_meta()) == learned
    assert learned.known_hidden == learned.hidden


def test_wikihow_o_texto_escondido_da_fonte_nao_levanta_flag() -> None:
    wikihow = load("wikihow_termometro.html")
    _, hidden = convert.prepare(wikihow)
    settings = Settings(database_url="postgresql+psycopg://x@localhost/x")
    learned = boilerplate.learn([wikihow] * 3)
    # a coautoria de outro artigo da mesma fonte: bem mais comprida (os blocos vão até 1100 letras)
    longer = hidden[0] + " Mais um parágrafo de apresentação do autor, com a sua carreira. " * 5

    for text in (*hidden, longer):  # o JSON das imagens nunca conta
        assert not flags.suspicious_hidden(text, learned.known_hidden)
    assert flags.suspicious_hidden(longer)  # sem o molde da fonte, prosa comprida é suspeita
    com = flags.detect("ok", hidden=hidden, settings=settings, known_hidden=learned.known_hidden)
    assert flags.HIDDEN not in {f.code for f in com}


# --- ZIM -------------------------------------------------------------------------------------


def test_aprende_o_boilerplate_de_um_zim_sem_o_percorrer(tmp_path: Path) -> None:
    articles = {
        f"Art{i}": (
            f"Art{i}",
            "<p>Voltar à página inicial do projeto Teste</p>"
            f"<p>Texto próprio do artigo número {i}, que mais ninguém tem.</p>",
        )
        for i in range(12)
    }
    source = ZimSource.open(
        make_zim(tmp_path / "teste_2026-01.zim", articles=articles, redirects={})
    )
    try:
        paths = source.random_paths(8)
        assert len(paths) == 8 == len(set(paths)) and set(paths) <= set(articles)
        assert 10 <= len(source.random_paths(40)) <= 12  # um ZIM pequeno dá o que tem
        assert source.random_paths(0) == []

        learned = boilerplate.learn_for_source(source, n=10)
    finally:
        source.close()

    assert learned.lines == frozenset({"voltar à página inicial do projeto teste"})
