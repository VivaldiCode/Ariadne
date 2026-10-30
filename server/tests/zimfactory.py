"""Cria ZIMs minúsculos para os testes, com `libzim.writer.Creator`."""

from pathlib import Path

import httpx
from libzim.reader import Archive
from libzim.writer import Creator, Hint, Item, StringProvider

PAGE = """<!DOCTYPE html>
<html lang="pt"><head><meta charset="UTF-8"><title>{title}</title>
<link rel="stylesheet" href="./_res_/style.css"><script>var lixo = 1;</script></head>
<body>
<nav><a href="./Outro">Navegação</a></nav>
<div id="mw-content-text">
{body}
</div>
<footer>Rodapé que não interessa</footer>
</body></html>"""

ARTICLES: dict[str, tuple[str, str]] = {
    "Lisboa": (
        "Lisboa",
        """<h1>Lisboa</h1>
<p>Lisboa é a capital de <a href="./Portugal">Portugal</a> e fica junto ao rio Tejo.
Veja também <a href="./Portugal#historia">a história</a>, a
<a href="Capital_de_Portugal">capital</a> e <a href="https://exemplo.org/fora">um site</a>.
<sup class="reference">[1]</sup></p>
<h2>História</h2>
<p>A cidade foi reconstruída depois do terramoto de 1755.</p>
<h3>Terramoto</h3>
<p>O terramoto de 1755 destruiu grande parte da cidade.</p>
<h2>Geografia</h2>
<ul><li>Rio Tejo</li><li>Sete colinas</li></ul>
<table class="navbox"><tr><td>Caixa de navegação</td></tr></table>
<div class="reflist"><ol class="references"><li>Referência escondida</li></ol></div>
<img src="./_res_/lisboa.png">""",
    ),
    "Portugal": (
        "Portugal",
        """<h1>Portugal</h1>
<p>Portugal é um país da Europa. A sua capital é <a href="./Lisboa">Lisboa</a>.</p>
<h2>História</h2><p>Fundado em 1143.</p>""",
    ),
    "Outro": ("Outro", "<p>Um artigo sem ligações.</p>"),
}
REDIRECTS = {"Capital_de_Portugal": ("Capital de Portugal", "Lisboa")}


class _Page(Item):
    def __init__(
        self, path: str, title: str, content: str | bytes, mimetype: str = "text/html"
    ) -> None:
        super().__init__()
        self._path, self._title, self._content, self._mimetype = path, title, content, mimetype

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._title

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._mimetype == "text/html"}


# UUID do ZIM -> livro do kiwix (o nome do ficheiro sem '.zim'), para os kiwix falsos dos testes.
ZIM_UUIDS: dict[str, str] = {}


def kiwix_books(params: httpx.QueryParams) -> list[str]:
    """Livros de um pedido /search, quer venham por nome (books.name) quer por UUID (books.id)."""
    return params.get_list("books.name") + [
        ZIM_UUIDS.get(book_id, book_id) for book_id in params.get_list("books.id")
    ]


def make_zim(
    path: Path,
    *,
    name: str | None = "wikipedia_pt_teste",
    title: str | None = "Wikipédia (teste)",
    language: str | None = "por",
    date: str | None = "2026-09-15",
    flavour: str | None = None,
    articles: dict[str, tuple[str, str]] | None = None,
    redirects: dict[str, tuple[str, str]] | None = None,
    resources: dict[str, tuple[str, bytes]] | None = None,
) -> Path:
    """`resources`: ficheiros não-HTML do ZIM (imagens): caminho -> (tipo, bytes)."""
    articles = ARTICLES if articles is None else articles
    redirects = REDIRECTS if redirects is None else redirects
    creator = Creator(path).config_indexing(True, language or "eng")
    with creator:
        for article_path, (article_title, body) in articles.items():
            creator.add_item(
                _Page(article_path, article_title, PAGE.format(title=article_title, body=body))
            )
        creator.add_item(_Page("_res_/style.css", "style", "body {}", "text/css"))
        for resource_path, (mimetype, data) in (resources or {}).items():
            creator.add_item(_Page(resource_path, resource_path, data, mimetype))
        for redirect_path, (redirect_title, target) in redirects.items():
            creator.add_redirection(redirect_path, redirect_title, target, {Hint.FRONT_ARTICLE: 1})
        creator.set_mainpath(next(iter(articles)))
        metadata = {
            "Name": name,
            "Title": title,
            "Language": language,
            "Date": date,
            "Flavour": flavour,
        }
        for key, value in metadata.items():
            if value is not None:
                creator.add_metadata(key, value)
        creator.add_metadata("Description", "ZIM de teste")
        creator.add_metadata("Creator", "testes")
        creator.add_metadata("Publisher", "testes")
    # O nível 1 pesquisa no kiwix pelo UUID do ZIM (books.id): os kiwix falsos resolvem-no por aqui.
    ZIM_UUIDS[str(Archive(path).uuid)] = path.name.removesuffix(".zim")
    return path
