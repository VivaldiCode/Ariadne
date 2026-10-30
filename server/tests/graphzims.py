"""Dois ZIMs minúsculos que falam do mesmo assunto, para o grafo entre fontes (Fase 5)."""

from pathlib import Path

from tests.zimfactory import make_zim

WIKIPEDIA = "zim:wikipedia_pt_teste"
WIKIBOOKS = "zim:wikibooks_pt_teste"
REVOLUTION = "Revolucao_dos_Cravos"
INTRO = (
    "A Revolução dos Cravos foi um golpe militar de 25 de abril de 1974 que derrubou o Estado "
    "Novo e abriu caminho à democracia em Portugal."
)


def _page(title: str, paragraph: str) -> tuple[str, str]:
    return (title, f"<h1>{title}</h1><p>{paragraph}</p><h2>Notas</h2><p>Mais texto de {title}.</p>")


WIKIPEDIA_ARTICLES = {
    REVOLUTION: _page("Revolução dos Cravos", INTRO),
    "Lisboa": _page(
        "Lisboa", "Lisboa é a capital de Portugal e fica junto ao rio Tejo, no litoral."
    ),
    "Cozinha": _page("Cozinha", "A cozinha portuguesa usa bacalhau, azeite e muito pouco picante."),
}
# 25 de Abril redireciona para o artigo: é um título alternativo da Revolução dos Cravos.
WIKIPEDIA_REDIRECTS = {"25_de_Abril": ("25 de Abril", REVOLUTION)}

WIKIBOOKS_ARTICLES = {
    REVOLUTION: _page("Revolução dos Cravos", "Capítulo sobre a revolução de abril de 1974."),
    "25_de_Abril": _page("25 de abril", "Guia de estudo sobre o dia da liberdade em Portugal."),
    "Receitas": _page("Receitas", "Livro de receitas tradicionais para fazer em casa com calma."),
}


def make_graph_dir(directory: Path) -> Path:
    """Pasta com 'wikipedia_pt_teste' e 'wikibooks_pt_teste' (livros em português)."""
    directory.mkdir(parents=True, exist_ok=True)
    make_zim(
        directory / "wikipedia_pt_teste_2026-09.zim",
        articles=WIKIPEDIA_ARTICLES,
        redirects=WIKIPEDIA_REDIRECTS,
    )
    make_zim(
        directory / "wikibooks_pt_teste_2026-09.zim",
        name="wikibooks_pt_teste",
        title="Wikilivros (teste)",
        articles=WIKIBOOKS_ARTICLES,
        redirects={},
    )
    return directory
