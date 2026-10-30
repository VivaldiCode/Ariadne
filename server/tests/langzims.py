"""ZIMs minúsculos PT e EN do mesmo tema («Revolução dos Cravos» / «Carnation Revolution»)."""

from pathlib import Path

from tests import sitelinks
from tests.zimfactory import make_zim

PT = "zim:wikipedia_pt_teste"
EN = "zim:wikipedia_en_teste"
BOOKS = "zim:wikibooks_pt_teste"

PT_CRAVOS = f"{PT}/Revolucao_dos_Cravos"
EN_CRAVOS = f"{EN}/Carnation_Revolution"
BOOKS_CRAVOS = f"{BOOKS}/Revolucao_dos_Cravos"
PT_LISBOA, EN_LISBON = f"{PT}/Lisboa", f"{EN}/Lisbon"
TRICKY_PT_PATH = "O'Neil_\\_(teste),_parte;_2"
PT_TRICKY, EN_TRICKY = f"{PT}/{TRICKY_PT_PATH}", f"{EN}/Tricky_title"

CRAVOS_PT_TEXT = (
    "A Revolução dos Cravos foi um golpe militar de 25 de abril de 1974 que derrubou o Estado "
    "Novo e abriu caminho à democracia em Portugal."
)
CRAVOS_EN_TEXT = (
    "The Carnation Revolution was a military coup on 25 April 1974 that overthrew the Estado "
    "Novo regime and opened the way to democracy in Portugal."
)


def _page(title: str, paragraph: str) -> tuple[str, str]:
    return (title, f"<h1>{title}</h1><p>{paragraph}</p><h2>Notas</h2><p>Mais texto de {title}.</p>")


PT_ARTICLES = {
    "Revolucao_dos_Cravos": _page("Revolução dos Cravos", CRAVOS_PT_TEXT),
    "Lisboa": _page("Lisboa", "Lisboa é a capital de Portugal e fica junto ao rio Tejo."),
    TRICKY_PT_PATH: _page(sitelinks.TRICKY_PT, "Um artigo com um título que parte parsers."),
    "Cozinha": _page("Cozinha", "A cozinha portuguesa usa bacalhau, azeite e pouco picante."),
}
EN_ARTICLES = {
    "Carnation_Revolution": _page("Carnation Revolution", CRAVOS_EN_TEXT),
    "Lisbon": _page("Lisbon", "Lisbon is the capital of Portugal and sits by the Tagus river."),
    "Tricky_title": _page("Tricky title", "An article with a title that breaks parsers."),
    "Cooking": _page("Cooking", "Cooking is the art of preparing food with heat."),
}
BOOKS_ARTICLES = {
    "Revolucao_dos_Cravos": _page("Revolução dos Cravos", "Capítulo sobre a revolução de abril."),
}


def make_lang_dir(directory: Path) -> Path:
    """Pasta com a Wikipédia PT, a Wikipédia EN e os Wikilivros PT (livros de teste)."""
    directory.mkdir(parents=True, exist_ok=True)
    make_zim(
        directory / "wikipedia_pt_teste_2026-09.zim",
        articles=PT_ARTICLES,
        redirects={"25_de_Abril": ("25 de Abril", "Revolucao_dos_Cravos")},
    )
    make_zim(
        directory / "wikipedia_en_teste_2026-09.zim",
        name="wikipedia_en_teste",
        title="Wikipedia (test)",
        language="eng",
        articles=EN_ARTICLES,
        redirects={},
    )
    make_zim(
        directory / "wikibooks_pt_teste_2026-09.zim",
        name="wikibooks_pt_teste",
        title="Wikilivros (teste)",
        articles=BOOKS_ARTICLES,
        redirects={},
    )
    return directory
