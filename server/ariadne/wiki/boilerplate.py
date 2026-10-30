"""O lixo repetido de cada fonte (menus, «Back to Home Page…», barra de línguas, logótipos).

Uma linha de texto, ou um `src` de imagem, que aparece em mais de 30% das páginas de uma amostra da
mesma fonte é navegação e não conteúdo (WIKI-IMPORT.md, secções 2.2 e 3). A amostra calcula-se
uma vez por fonte (`learn_for_source`) e guarda-se em JSON (`to_meta`/`from_meta`).

«Linha» é o que o leitor vê como uma unidade de texto: o conteúdo de um bloco, ou de um troço entre
dois `<br>`, sem os blocos aninhados. A mesma noção (`runs`) serve para aprender e para tirar, por
isso o que se aprende é exatamente o que se consegue tirar.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from ariadne.curation.flags import hidden_shape, is_data
from ariadne.sources.zim import content_root
from ariadne.wiki import titles as _titles

if TYPE_CHECKING:  # evita o ciclo: `convert` importa este módulo
    from ariadne.sources.zim import ZimSource

RULES = 2  # versão das regras de aprendizagem: muda-se quando o que se aprende muda (reaprende-se)
MIN_PAGES = 5  # abaixo disto a amostra não distingue menu de coincidência
MIN_HIDDEN_PAGES = 3  # com menos páginas nem os moldes do texto escondido se aprendem
SHAPE_MIN_WORDS = 4  # um molde precisa de palavras (os rótulos curtos nunca levantam flag)
MIN_COUNT = 3  # e a linha tem de estar em pelo menos 3 páginas, qualquer que seja a fração
MIN_LINE = 8  # «Home» sozinho não é lixo: é uma palavra comum
MAX_LINE = 600
MAX_BLOCK = 1500  # um bloco de ligações maior que isto já é conteúdo (um índice)
LINK_RATIO = 0.8  # fração do texto de uma linha/bloco dentro de ligações para ser «de ligações»
BANNER_TEXT = 40  # letras de texto (fora de ligações) a partir das quais já não é o topo
EDGE_RUNS = 4  # as primeiras e últimas linhas com texto são o sítio das barras de navegação
SAMPLE_BIG = 60  # artigos lidos para aprender o lixo de uma fonte grande
SAMPLE_SMALL = 150  # e de uma fonte pequena (a amostra aumenta: a repetição vê-se melhor)
SMALL_SOURCE = 10_000  # artigos
_SPACES = re.compile(r"\s+")
_UP = re.compile(r"^(?:\.{1,2}/)+")  # «../../» e «./» à frente de um caminho relativo
# Os separadores de uma barra de ligações: «Home - English | French» e «Home • English • French»
# são a mesma barra.
_SEPARATORS = re.compile(r"[\s\-–—•·|/\\»›«‹<>:;,]+")
_WORDS = re.compile(r"[^\W_]+")
# Palavras que ligam as ligações de uma barra («Back to Home or Back to list»).
_CONNECTORS = frozenset({"or", "and", "ou", "e", "y", "et", "und", "oder", "o", "of", "the"})
_BANNER_NAME = re.compile(r"logo|banner|masthead|sitelogo|header", re.IGNORECASE)
_LINK_CONTAINERS = ["ul", "ol", "nav", "table", "div", "p", "center", "dl", "td"]
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# Elementos que partem uma linha de texto: o texto de um bloco não se mistura com o do vizinho.
BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details",
        "dir", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "hr", "html", "li", "main", "menu", "nav", "ol", "p",
        "pre", "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)  # fmt: skip


def normalize(text: str) -> str:
    """O texto em minúsculas, sem formas de compatibilidade nem espaços repetidos: a chave."""
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


def image_key(src: str) -> str:
    """O `src` sem os «../» da frente e sem *query*: o mesmo logótipo vem com profundidades
    diferentes conforme a página, e o nome do ficheiro não muda."""
    path = unquote(urlsplit(src.strip()).path)
    return _UP.sub("", path).casefold()


@dataclass(frozen=True)
class Boilerplate:
    """As linhas (normalizadas) e as imagens (`image_key`) que são lixo de uma fonte."""

    lines: frozenset[str] = frozenset()
    images: frozenset[str] = frozenset()
    # Os moldes (`flags.hidden_shape`) do texto escondido que a fonte repete: estrutura, não ataque.
    hidden: frozenset[str] = frozenset()
    # O nome do site (prefixo ou sufixo comum dos títulos) e os títulos que se repetem em muitas
    # páginas da fonte (`titles.learn_names`): o título da página não os usa.
    site: frozenset[str] = frozenset()
    generic: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.lines or self.images or self.hidden or self.site or self.generic)

    @property
    def known_hidden(self) -> frozenset[str]:
        """O que o texto escondido pode ser sem levantar a flag `hidden_text`: as linhas e os
        moldes que a fonte repete."""
        return self.lines | self.hidden

    def to_meta(self) -> dict[str, list[str]]:
        """Para guardar em JSON (ordenado: o mesmo conteúdo dá sempre o mesmo texto)."""
        return {
            "lines": sorted(self.lines),
            "images": sorted(self.images),
            "hidden": sorted(self.hidden),
            "site": sorted(self.site),
            "generic": sorted(self.generic),
        }

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any] | None) -> "Boilerplate":
        """O contrário de `to_meta`; aceita `None` ou um dicionário incompleto (sem lixo)."""
        if not meta:
            return cls()
        return cls(
            frozenset(str(v) for v in meta.get("lines") or ()),
            frozenset(str(v) for v in meta.get("images") or ()),
            frozenset(str(v) for v in meta.get("hidden") or ()),
            frozenset(str(v) for v in meta.get("site") or ()),
            frozenset(str(v) for v in meta.get("generic") or ()),
        )

    def is_image(self, src: str) -> bool:
        return image_key(src) in self.images


def to_meta(boilerplate: Boilerplate) -> dict[str, list[str]]:
    return boilerplate.to_meta()


def from_meta(meta: Mapping[str, Any] | None) -> Boilerplate:
    return Boilerplate.from_meta(meta)


# --- linhas ---------------------------------------------------------------------------------


def runs(root: BeautifulSoup | Tag) -> Iterator[tuple[Tag | BeautifulSoup, list[Any], str]]:
    """As «linhas» de texto da árvore: (contentor, nós, texto normalizado). Uma linha vai de um
    `<br>` ou de um bloco ao seguinte, dentro do mesmo contentor; os blocos aninhados são linhas
    do seu próprio contentor. Os nós que não são texto nem etiquetas (comentários) não contam."""
    containers: list[Tag | BeautifulSoup] = [root, *root.find_all(True)]
    for container in containers:
        current: list[Any] = []
        for child in list(container.children):
            if isinstance(child, Tag) and (child.name in BLOCK_TAGS or child.name == "br"):
                if current:
                    yield container, current, _text_of(current)
                    current = []
            elif isinstance(child, Tag) or type(child) is NavigableString:
                current.append(child)
        if current:
            yield container, current, _text_of(current)


def _text_of(nodes: Sequence[Any]) -> str:
    parts = [n.get_text(" ") if isinstance(n, Tag) else str(n) for n in nodes]
    return normalize(" ".join(parts))


def link_key(text: str) -> str:
    """A chave de uma linha ou bloco de ligações: sem separadores (`-`, `•`, `|`, `/`…) nem
    espaços repetidos, para a mesma barra casar com ou sem eles."""
    return _SEPARATORS.sub(" ", normalize(text)).strip()


def _compact(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def _anchors(nodes: Sequence[Any]) -> list[Tag]:
    found: list[Tag] = []
    for node in nodes:
        if isinstance(node, Tag):
            if node.name == "a":
                found.append(node)
            else:
                found.extend(node.find_all("a"))
    return found


def _top_level(anchors: Sequence[Tag]) -> list[Tag]:
    return [a for a in anchors if a.find_parent("a") is None]


def _link_share(nodes: Sequence[Any]) -> tuple[int, float]:
    """(número de ligações, fração do texto que está dentro delas) de uma linha."""
    anchors = _top_level(_anchors(nodes))
    total = sum(_compact(n.get_text() if isinstance(n, Tag) else str(n)) for n in nodes)
    inside = sum(_compact(a.get_text()) for a in anchors)
    return len(anchors), inside / total if total else 0.0


def is_link_line(nodes: Sequence[Any]) -> bool:
    """Uma linha que é (quase) só ligações: duas ou mais, e 80% do texto dentro delas."""
    count, share = _link_share(nodes)
    return count >= 2 and share >= LINK_RATIO


def is_link_block(element: Tag) -> bool:
    """Um bloco (lista, tabela, `div`…) que é (quase) só ligações: três ou mais."""
    if len(element.get_text()) > MAX_BLOCK:
        return False
    count, share = _link_share([element])
    return count >= 3 and share >= LINK_RATIO


def _link_features(root: BeautifulSoup | Tag) -> set[str]:
    """As chaves (`link_key`) das linhas e dos blocos de ligações da página."""
    keys: set[str] = set()
    for _, nodes, text in runs(root):
        if is_link_line(nodes) and MIN_LINE <= len(link_key(text)) <= MAX_LINE:
            keys.add(link_key(text))
    for element in root.find_all(_LINK_CONTAINERS):
        if is_link_block(element):
            key = link_key(element.get_text(" "))
            if len(key) >= MIN_LINE:
                keys.add(key)
    return keys


def _countable(container: Tag | BeautifulSoup, text: str) -> bool:
    if not MIN_LINE <= len(text) <= MAX_LINE or not any(c.isalpha() for c in text):
        return False
    if isinstance(container, Tag):
        # um título («Referências») repete-se em todos os artigos e é estrutura, não lixo; as
        # células de uma caixa de informação («Nascimento») também
        if container.name in _HEADINGS or container.find_parent(_HEADINGS) is not None:
            return False
        if container.find_parent("table", class_="infobox") is not None:
            return False
    return True


def strip(root: BeautifulSoup | Tag, boilerplate: Boilerplate) -> int:
    """Tira da árvore as linhas e os blocos de ligações que são lixo da fonte e devolve quantos
    tirou. Uma linha de ligações compara-se sem separadores (`link_key`)."""
    if not boilerplate.lines:
        return 0
    lines, removed = boilerplate.lines, 0
    for element in root.find_all(_LINK_CONTAINERS):  # os blocos (de fora para dentro)
        if element.decomposed or not is_link_block(element):
            continue
        if link_key(element.get_text(" ")) in lines:
            element.decompose()
            removed += 1
    found = [
        nodes
        for _, nodes, text in runs(root)
        if text in lines or (is_link_line(nodes) and link_key(text) in lines)
    ]
    for nodes in found:
        for node in nodes:
            node.extract()
    return removed + len(found)


def _is_bar(nodes: Sequence[Any]) -> bool:
    """Ligações separadas só por separadores e palavras de ligação («Home - English - French»,
    «Back to X or Back to Y»): uma barra de navegação."""
    anchors = _top_level(_anchors(nodes))
    if len(anchors) < 2:
        return False
    anchor_ids = {id(a) for a in anchors}
    for node in nodes:
        for string in node.strings if isinstance(node, Tag) else [node]:
            if any(id(parent) in anchor_ids for parent in string.parents):
                continue
            if any(w.casefold() not in _CONNECTORS for w in _WORDS.findall(str(string))):
                return False
    return True


def _is_external_image_link(nodes: Sequence[Any]) -> bool:
    """Só uma imagem dentro de uma ligação para fora («Donate», patrocinadores): publicidade da
    fonte. Uma imagem ligada a uma página interna (uma miniatura) é conteúdo."""
    anchors = _top_level(_anchors(nodes))
    if not anchors or any(a.find("img") is None for a in anchors):
        return False
    if any(_compact(n.get_text() if isinstance(n, Tag) else str(n)) for n in nodes):
        return False  # tem texto: não é só a imagem
    return all(
        str(a.get("href") or "").lower().startswith(("http://", "https://")) for a in anchors
    )


def strip_link_bars(root: BeautifulSoup | Tag) -> int:
    """Tira as barras de ligações que ficam no topo ou no fim do texto (a barra de línguas e o
    «Back to Home Page…» do cd3wd) e as imagens-ligação para fora do fim («Donate»), mesmo que a
    amostra da fonte não as tenha visto."""
    texty = [
        (nodes, text)
        for _, nodes, text in runs(root)
        if any(c.isalpha() for c in text) or _is_external_image_link(nodes)
    ]
    edges = texty[:EDGE_RUNS] + texty[max(EDGE_RUNS, len(texty) - EDGE_RUNS) :]
    bars = [nodes for nodes, _ in edges if _is_bar(nodes) or _is_external_image_link(nodes)]
    for nodes in bars:
        for node in nodes:
            node.extract()
    return len(bars)


def strip_banner(root: BeautifulSoup | Tag) -> int:
    """Tira o logótipo do site: uma imagem com «logo», «banner»… no nome do ficheiro que vem
    antes de qualquer texto do artigo (e não está numa tabela nem numa figura)."""
    seen, doomed = 0, []
    for node in root.descendants:
        if isinstance(node, Tag):
            if node.name != "img" or seen >= BANNER_TEXT:
                continue
            src = node.get("src")
            name = image_key(src).rsplit("/", 1)[-1] if isinstance(src, str) else ""
            if _BANNER_NAME.search(name) and node.find_parent(["table", "figure"]) is None:
                doomed.append(node)
        elif type(node) is NavigableString and node.find_parent("a") is None:
            seen += _compact(str(node))
    for img in doomed:
        img.decompose()
    return len(doomed)


def _page_features(html: str) -> tuple[set[str], set[str], set[str], set[str]]:
    """(linhas, imagens, moldes do texto escondido, linhas e blocos de ligações) de uma página."""
    from ariadne.wiki.convert import hidden_texts  # aqui para não criar um ciclo ao importar

    root = content_root(html)
    shapes = {
        hidden_shape(t)
        for t in hidden_texts(root)
        if len(t.split()) >= SHAPE_MIN_WORDS and not is_data(t)
    }
    for title in root.find_all("h1"):
        title.decompose()
    lines = {text for c, _, text in runs(root) if _countable(c, text)}
    images = {
        image_key(src)
        for img in root.find_all("img")
        if isinstance(src := img.get("src"), str) and src and not src.startswith("data:")
    }
    return lines, images, shapes, _link_features(root)


def learn(
    htmls: Sequence[str], threshold: float = 0.3, zim_titles: Sequence[str] | None = None
) -> Boilerplate:
    """O lixo de uma fonte, a partir de uma amostra das suas páginas: o que está em mais de
    `threshold` delas. As linhas e os blocos de ligações (menus, barras de línguas) chegam a 3
    páginas, qualquer que seja a fração, e comparam-se sem separadores. Com menos de `MIN_PAGES`
    páginas só se aprendem os moldes do texto escondido e o nome do site dos títulos. `zim_titles`
    são os títulos que o ZIM dá a cada página (por omissão, o `<title>`)."""
    if len(htmls) < MIN_HIDDEN_PAGES:
        return Boilerplate()
    line_count: Counter[str] = Counter()
    image_count: Counter[str] = Counter()
    shape_count: Counter[str] = Counter()
    link_count: Counter[str] = Counter()
    for html in htmls:
        lines, images, shapes, links = _page_features(html)
        line_count.update(lines)
        image_count.update(images)
        shape_count.update(shapes)
        link_count.update(links)
    # O texto escondido que se repete em 3 páginas é molde da fonte (a flag `hidden_text` ignora-o);
    # não precisa dos 30%, só afeta essa flag, e uma injeção de verdade apanha-se pelo léxico.
    hidden = frozenset(t for t, n in shape_count.items() if n >= MIN_COUNT)
    raws = [_titles.page_titles(h) for h in htmls]
    if zim_titles is not None and len(zim_titles) == len(raws):
        raws = [
            _titles.Raw(r.h1, r.og, _titles.tidy(z) if z else r.title)
            for r, z in zip(raws, zim_titles, strict=True)
        ]
    site, generic = _titles.learn_names(raws)
    if len(htmls) < MIN_PAGES:
        return Boilerplate(hidden=hidden, site=site, generic=generic)
    limit = max(MIN_COUNT - 1, threshold * len(htmls))
    nav = frozenset(t for t, n in link_count.items() if n >= MIN_COUNT)
    return Boilerplate(
        frozenset(t for t, n in line_count.items() if n > limit) | nav,
        frozenset(k for k, n in image_count.items() if n > limit),
        hidden,
        site,
        generic,
    )


def sample_size(article_count: int) -> int:
    """Quantos artigos ler para aprender o lixo de uma fonte: numa fonte pequena lê-se (quase)
    tudo, porque com poucos artigos uma amostra de 40 pode nem ver a barra de uma secção."""
    if article_count <= 0:
        return SAMPLE_BIG
    if article_count <= SMALL_SOURCE:
        return min(article_count, SAMPLE_SMALL)
    return SAMPLE_BIG


def learn_for_source(source: "ZimSource", n: int | None = None) -> Boilerplate:
    """O lixo do ZIM, a partir de `n` artigos aleatórios (sem o percorrer: serve a Wikipédia).
    Sem `n`, o tamanho da amostra depende do tamanho da fonte (`sample_size`)."""
    htmls: list[str] = []
    zim_titles: list[str] = []
    for path in source.random_paths(n if n is not None else sample_size(source.article_count())):
        try:
            article = source.read(path)
        except Exception:  # um artigo ilegível não impede de aprender com os outros
            continue
        htmls.append(article.html)
        zim_titles.append(article.title)
    return learn(htmls, zim_titles=zim_titles)
