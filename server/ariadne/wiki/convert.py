"""Conversão determinística do HTML do ZIM em Markdown (sem modelo), para o Wiki.js
(WIKI-IMPORT.md, secções 2 e 3).

`prepare` limpa o que não é conteúdo (menus, avisos, referências: `content_root`), tira o texto
escondido (`display:none`, `font-size:0`…), o lixo repetido da fonte (`Boilerplate`), refaz os
parágrafos, títulos e listas dos textos que só têm `<br>`, tira os títulos vazios e a linha de
licença do ZIM e transforma a caixa de informação numa lista. `to_markdown` escreve o Markdown
nativo do Wiki.js (tabelas, avisos `{.is-info}`, código com língua, figuras com legenda) e reescreve
os links internos para o destino que o importador decidiu (a página do Wiki.js se o artigo lá
está, senão o kiwix). As imagens só entram se o importador as enviou ao Wiki.js (`images`).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from markdownify import MarkdownConverter

from ariadne.sources.zim import content_root
from ariadne.wiki import boilerplate as _boilerplate
from ariadne.wiki.boilerplate import Boilerplate

_BLANK_LINES = re.compile(r"\n{3,}")
_BLANK_SPACES = re.compile(r"^[ \t]+$", re.MULTILINE)  # linhas só com espaços
_EDGE_RULES = re.compile(r"\A(?:\s*---\s*)+|(?:\s*---\s*)+\Z")  # a linha que sobrou do menu
_EXTERNAL = re.compile(r"^(?:https?://|mailto:)", re.IGNORECASE)
_HIDING_CLASSES = frozenset(
    {"sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text", "screen-reader-only"}
)
_COLORS = {
    "white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0), "green": (0, 128, 0),
    "blue": (0, 0, 255), "yellow": (255, 255, 0), "gray": (128, 128, 128), "grey": (128, 128, 128),
    "silver": (192, 192, 192), "transparent": (-1, -1, -1),
}  # fmt: skip
_LENGTH = re.compile(r"^(-?\d*\.?\d+)\s*(px|pt|em|rem|%|vw|vh|ex|ch)?$")


def _color(value: str) -> tuple[int, ...] | None:
    """Uma cor CSS (nome, #rgb, #rrggbb, rgb(...)) como (r, g, b); `None` se não percebe."""
    value = value.strip().lower().replace("!important", "").strip()
    if value in _COLORS:
        return _COLORS[value]
    if match := re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value):
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))
    if match := re.fullmatch(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)[^)]*\)", value):
        return tuple(int(g) for g in match.groups())
    return None


def _length(value: str) -> tuple[float, str] | None:
    match = _LENGTH.match(value.strip().lower().replace("!important", "").strip())
    return (float(match.group(1)), match.group(2) or "") if match else None


def _style(tag: Tag) -> dict[str, str]:
    raw = tag.get("style")
    if not isinstance(raw, str):
        return {}
    pairs = (part.split(":", 1) for part in raw.split(";") if ":" in part)
    return {k.strip().lower(): v.strip().lower() for k, v in pairs}


def _off_screen(style: dict[str, str]) -> bool:
    for prop in ("left", "right", "top", "bottom", "text-indent", "margin-left", "margin-top"):
        parsed = _length(style.get(prop, ""))
        if parsed and parsed[0] <= -500:
            return True
    return False


def _tiny(style: dict[str, str]) -> bool:
    size = _length(style.get("font-size", ""))
    if size is None:
        return False
    value, unit = size
    return value <= {"px": 1, "pt": 1, "": 0, "em": 0.1, "rem": 0.1, "%": 5, "ex": 0.1}.get(unit, 0)


def is_hidden(tag: Tag) -> bool:
    """O elemento não se vê: `hidden`, `aria-hidden`, classes de «só leitores de ecrã», estilo que
    o esconde (display, visibility, opacity, fonte minúscula, fora do ecrã, recorte) ou texto da
    cor do fundo (cores equivalentes: #fff = #ffffff = white = rgb(255,255,255))."""
    if tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).lower() == "true":
        return True
    classes = tag.get("class")
    if isinstance(classes, list) and _HIDING_CLASSES.intersection(c.lower() for c in classes):
        return True
    style = _style(tag)
    if not style:
        return False
    if style.get("display") == "none" or style.get("visibility") in ("hidden", "collapse"):
        return True
    opacity = _length(style.get("opacity", ""))
    if opacity and not opacity[1] and opacity[0] <= 0.02:
        return True
    if _tiny(style) or _off_screen(style):
        return True
    if style.get("clip", "").replace(" ", "").startswith("rect(0") or "inset(100%" in style.get(
        "clip-path", ""
    ):
        return True
    for prop in ("width", "height"):
        parsed = _length(style.get(prop, ""))
        if parsed and parsed[0] == 0 and style.get("overflow") == "hidden":
            return True
    color = _color(style.get("color", ""))
    background = _color(style.get("background-color", style.get("background", "")))
    return (color is not None and color == background) or color == (-1, -1, -1)


def hidden_texts(root: BeautifulSoup | Tag) -> list[str]:
    """Remove da árvore os elementos escondidos e devolve o texto que continham (para a triagem)."""
    found: list[str] = []
    for tag in list(root.find_all(True)):
        if tag.decomposed or not is_hidden(tag):
            continue
        text = " ".join(tag.get_text(" ").split())
        if text:
            found.append(text)
        tag.decompose()
    return found


@dataclass(frozen=True)
class ImageRef:
    """Uma imagem do artigo: o `src` tal como está no HTML, o texto alternativo e a legenda."""

    src: str
    alt: str = ""
    caption: str = ""


@dataclass(frozen=True)
class FormatQuality:
    """Se a página ficou com forma suficiente depois das regras, ou se vai para o lote LLM."""

    needs_rescue: bool
    reason: str = ""


_SPACES = re.compile(r"\s+")
_FACTORY = BeautifulSoup("", "html.parser")  # só para criar etiquetas novas
_BLOCK_TAGS = _boilerplate.BLOCK_TAGS
_NO_REFLOW = frozenset({"pre", "code", "kbd", "samp", "textarea", "script", "style"})


def _new(name: str, *children: Any) -> Tag:
    tag = _FACTORY.new_tag(name)
    for child in children:
        tag.append(child)
    return tag


def _plain(node: Tag) -> str:
    """O texto de um elemento numa linha, sem espaços antes da pontuação."""
    text = _SPACES.sub(" ", node.get_text(" ")).strip()
    return re.sub(r"\s+([,.;:!?)\]])", r"\1", text)


# --- refluxo: texto que só tem <br> (secção 2.1) ---------------------------------------------

_MIN_BR = 6  # com menos quebras o autor quis mesmo as quebras (versos, moradas)
_MIN_LINE = 30  # linhas médias abaixo disto, sem parágrafos, são uma lista de links e não texto
_NOISE_LINE = re.compile(r"^\S+\.(?:gif|jpe?g|png|bmp|tiff?|webp)\s*\(\d+\s*x\s*\d+\)$", re.I)
_BULLET = re.compile(r"^([-•·▪●■◦*–]|\d{1,3}\)|[a-z]\))\s+(\S.*)$")
_NESTED_BULLET = re.compile(r"^o\s+(\S.*)$")  # «o » é só marcador dentro de uma lista
_MARKER_PREFIX = re.compile(r"^\s*(?:[-•·▪●■◦*–]|\d{1,3}\)|[a-z]\)|o)\s+")
_KEYWORD_HEADING = re.compile(
    r"^(?:cap[íi]tulo|chapter|chapitre|se[çc][ãa]o|section|parte|part|anexo|ap[êe]ndice|"
    r"appendix|artigo)\s+[\divxlc]+\b",
    re.I,
)
_NUMBERED = "data-numbered"
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
_SENTENCE_END = (".", ",", ";", ":", "?", "!")


@dataclass
class _Line:
    nodes: list[Any]
    text: str  # com os espaços colapsados
    has_img: bool

    @property
    def blank(self) -> bool:
        return not self.text and not self.has_img


def _line(nodes: list[Any]) -> _Line:
    parts = [n.get_text("") if isinstance(n, Tag) else str(n) for n in nodes]
    has_img = any(isinstance(n, Tag) and (n.name == "img" or n.find("img")) for n in nodes)
    return _Line(nodes, _SPACES.sub(" ", "".join(parts)).strip(), has_img)


def _segments(container: Tag | BeautifulSoup) -> list[list[_Line] | Tag]:
    """Os filhos do contentor: troços de linhas (separadas por `<br>`) e, no sítio, os blocos."""
    out: list[list[_Line] | Tag] = []
    lines: list[_Line] = []
    current: list[Any] = []
    for child in list(container.children):
        if isinstance(child, Tag) and (child.name == "br" or child.name in _BLOCK_TAGS):
            lines.append(_line(current))
            current = []
            if child.name != "br":
                out.append(lines)
                out.append(child)
                lines = []
        elif isinstance(child, Tag) or type(child) is NavigableString:
            current.append(child)
    lines.append(_line(current))
    out.append(lines)
    return out


def _is_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 4 and len(text) <= 100 and not any(c.islower() for c in letters)


def _heading_level(text: str) -> int | None:
    """O nível do título que uma linha isolada é, ou `None` se é só texto."""
    if len(text) > 100 or text.endswith(_SENTENCE_END):
        return None
    if _is_caps(text) or (_KEYWORD_HEADING.match(text) and len(text) <= 60):
        return 2
    if match := _NUMBERED_HEADING.match(text):
        rest = match.group(2)
        if len(text) <= 90 and rest[0].isupper() and len(rest.split()) <= 12:
            return min(1 + match.group(1).count(".") + 1, 4)
    return None


def _strings(nodes: list[Any]) -> list[NavigableString]:
    """Os textos não vazios dos nós da linha, pela ordem em que aparecem."""
    return [
        s
        for node in nodes
        for s in ([node] if type(node) is NavigableString else node.find_all(string=True))
        if type(s) is NavigableString and str(s).strip()
    ]


def _retext(line: _Line, old: NavigableString, text: str) -> None:
    """Troca um texto da linha por outro; se era um dos nós da própria linha, a lista acompanha."""
    new = NavigableString(text)
    old.replace_with(new)
    line.nodes = [new if node is old else node for node in line.nodes]


def _strip_marker(line: _Line, rest: str) -> None:
    """Tira o marcador de lista («- », «1) ») do início dos nós da linha."""
    strings = _strings(line.nodes)
    if strings:
        _retext(line, strings[0], _MARKER_PREFIX.sub("", str(strings[0]), count=1))
    line.text = rest


def _last_string(tag: Tag) -> NavigableString | None:
    for string in reversed(tag.find_all(string=True)):
        if type(string) is NavigableString and str(string).strip():
            return string
    return None


def _hyphenated(previous: str, following: str) -> bool:
    """«manuten-» + «ção»: uma palavra partida pela hifenização, não um composto («norte-»)."""
    return (
        len(previous) > 1
        and previous.endswith("-")
        and previous[-2].isalpha()
        and following[:1].isalpha()
        and following[:1].islower()
    )


def _tidy(tag: Tag) -> None:
    """Espaços colapsados (o Wiki.js trata a quebra de linha do texto como quebra de linha),
    mesmo entre nós vizinhos, e sem espaços nas pontas."""
    after_space = True  # o início da etiqueta conta como depois de um espaço
    last: NavigableString | None = None
    for string in list(tag.find_all(string=True)):
        if type(string) is not NavigableString:
            continue
        text = _SPACES.sub(" ", str(string))
        if after_space:
            text = text.lstrip(" ")
        if text:
            after_space = text.endswith(" ")
        if not text:  # um texto vazio entre dois nós faz o markdownify comer o espaço anterior
            string.extract()
            continue
        replacement = NavigableString(text)
        string.replace_with(replacement)
        if text.strip():
            last = replacement
    if last is not None:
        last.replace_with(NavigableString(str(last).rstrip()))


def _trim(line: _Line) -> None:
    """Tira os espaços do princípio e do fim da linha (o recuo do original e a quebra de linha)."""
    strings = _strings(line.nodes)
    if not strings:
        return
    first, last = strings[0], strings[-1]
    if first is last:
        _retext(line, first, str(first).strip())
        return
    _retext(line, first, str(first).lstrip())
    _retext(line, last, str(last).rstrip())


def _fill(tag: Tag, lines: list[_Line]) -> Tag:
    """Junta as linhas na etiqueta: com um espaço, ou sem nada se a palavra foi hifenizada."""
    previous = ""
    for index, line in enumerate(lines):
        _trim(line)
        if index:
            if _hyphenated(previous, line.text):
                last = _last_string(tag)
                if last is not None:
                    last.replace_with(NavigableString(str(last).rstrip()[:-1]))
            else:
                tag.append(NavigableString(" "))
        for node in line.nodes:
            tag.append(node)
        previous = line.text
    _tidy(tag)
    return tag


@dataclass
class _Item:
    ordered: bool
    lines: list[_Line]
    children: list["_Item"]


def _render_list(items: list[_Item]) -> Tag:
    tag = _new("ol" if items[0].ordered else "ul")
    for item in items:
        entry = _fill(_new("li"), item.lines)
        if item.children:
            entry.append(_render_list(item.children))
        tag.append(entry)
    return tag


def _text_blocks(lines: list[_Line]) -> list[Tag]:
    """Parágrafos e listas de um grupo de linhas seguidas (sem linhas em branco pelo meio)."""
    out: list[Tag] = []
    paragraph: list[_Line] = []
    items: list[_Item] = []

    def flush() -> None:
        if paragraph:
            out.append(_fill(_new("p"), paragraph))
            paragraph.clear()
        if items:
            out.append(_render_list(items))
            items.clear()

    for line in lines:
        bullet = _BULLET.match(line.text)
        nested = _NESTED_BULLET.match(line.text) if items and not bullet else None
        if bullet:
            ordered = bullet.group(1)[0].isalnum()
            if paragraph or (items and items[0].ordered != ordered):
                flush()
            _strip_marker(line, bullet.group(2))
            items.append(_Item(ordered, [line], []))
        elif nested:
            _strip_marker(line, nested.group(1))
            items[-1].children.append(_Item(False, [line], []))
        elif items:
            last = items[-1].children[-1] if items[-1].children else items[-1]
            ends = last.lines[-1].text.endswith(_SENTENCE_END)
            if line.text[:1].islower() or not ends:
                last.lines.append(line)  # a linha do item que continuou na seguinte
            else:
                flush()
                paragraph.append(line)
        else:
            paragraph.append(line)
    flush()
    return out


def _group_blocks(group: list[_Line]) -> list[Tag]:
    """Um grupo de linhas entre duas linhas em branco: título, parágrafo(s) e listas."""
    if len(group) == 1 and (level := _heading_level(group[0].text)):
        heading = _fill(_new(f"h{level}"), group)
        if not _is_caps(group[0].text):
            heading[_NUMBERED] = "1"  # um índice tem muitos destes seguidos (`_toc_lists`)
        return [heading]
    lead = 0
    while lead < len(group) and _is_caps(group[lead].text):
        lead += 1
    rest = group[lead:]
    caps_text = " ".join(line.text for line in group[:lead])
    if (
        0 < lead <= 3
        and len(caps_text) <= 160
        and not (rest and rest[0].text[:1].islower())
        and not (lead == len(group) and caps_text.endswith("."))
    ):
        return [_fill(_new("h2"), group[:lead]), *(_text_blocks(rest) if rest else [])]
    return _text_blocks(group)


def _segment_blocks(lines: list[_Line]) -> list[Tag]:
    out: list[Tag] = []
    group: list[_Line] = []

    def flush() -> None:
        if group:
            out.extend(_group_blocks(group))
            group.clear()

    for line in lines:
        if _NOISE_LINE.match(line.text):  # «aom1.gif (600x600)», o resto da exportação do Word
            line.nodes, line.text, line.has_img = [], "", False
        if line.blank:
            flush()
        elif not line.text:  # só imagens
            flush()
            out.append(_fill(_new("p"), [line]))
        else:
            group.append(line)
    flush()
    return _toc_lists(out)


def _toc_lists(blocks: list[Tag]) -> list[Tag]:
    """Quatro ou mais títulos numerados seguidos, sem texto no meio, são um índice: viram lista."""
    out: list[Tag] = []
    run: list[Tag] = []

    def flush() -> None:
        if len(run) >= 4:
            entries = _new("ul")
            for heading in run:
                entry = _new("li")
                for child in list(heading.contents):
                    entry.append(child)
                entries.append(entry)
            out.append(entries)
        else:
            for heading in run:
                del heading[_NUMBERED]
            out.extend(run)
        run.clear()

    for block in blocks:
        if block.has_attr(_NUMBERED):
            run.append(block)
        else:
            flush()
            out.append(block)
    flush()
    return out


def _reflow_container(container: Tag | BeautifulSoup) -> None:
    segments = _segments(container)
    texts = [line.text for seg in segments if isinstance(seg, list) for line in seg if line.text]
    blanks = sum(1 for seg in segments if isinstance(seg, list) for line in seg if line.blank)
    if not texts or (blanks == 0 and sum(map(len, texts)) / len(texts) < _MIN_LINE):
        return
    rebuilt: list[Tag] = []
    for seg in segments:
        if isinstance(seg, Tag):
            rebuilt.append(seg)
        else:
            rebuilt.extend(_segment_blocks(seg))
    container.clear()
    for node in rebuilt:
        container.append(node)


def _reflow(root: BeautifulSoup | Tag) -> None:
    """Os blocos com mais `<br>` do que blocos filhos (texto exportado do Word, de TXT…) voltam a
    ter parágrafos, títulos e listas. `<br><br>` (ou uma linha em branco) separa parágrafos, uma
    quebra simples junta a linha à seguinte, e uma palavra hifenizada no fim da linha refaz-se."""
    for container in [root, *root.find_all(True)]:
        brs = sum(1 for c in container.children if isinstance(c, Tag) and c.name == "br")
        if brs < _MIN_BR:
            continue
        blocks = sum(1 for c in container.children if isinstance(c, Tag) and c.name in _BLOCK_TAGS)
        if brs <= blocks:
            continue
        if isinstance(container, Tag) and (
            container.name in _NO_REFLOW or container.find_parent(_NO_REFLOW) is not None
        ):
            continue
        _reflow_container(container)


# --- limpeza: licença do ZIM e o que ficou vazio (secção 2.4) ---------------------------------

_LICENSE = re.compile(
    r"^\s*(?:this (?:article|page) is issued from|este artigo é emitido por)\b", re.I
)
_KEEPS_CONTENT = ("img", "table", "pre", "hr", "video", "audio", "math", "svg", "iframe")
_PRUNABLE = ["p", "div", "span", "li", "ul", "ol", "section", "font", "center", "a", "b", "i", "em"]
_HEADING_NAMES = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _drop_license(root: BeautifulSoup | Tag) -> None:
    """Sai a linha de licença que o mwoffliner põe no fim («Este artigo é emitido por…»): o
    rodapé da página do Wiki.js já tem a licença da fonte."""
    for tag in root.select(".zim-footer"):
        tag.decompose()
    for tag in list(root.find_all(["p", "div", "li"])):
        if not tag.decomposed and len(tag.get_text()) < 800 and _LICENSE.match(tag.get_text()):
            tag.decompose()


# --- limpeza: pedidos de donativos (carteiras de criptomoedas) ----------------------------------

_CRYPTO_NAME = re.compile(r"\b(?:monero|bitcoin|ether(?:eum)?|litecoin|xmr|btc|eth|ltc)\b", re.I)
_CRYPTO_ADDRESS = re.compile(
    r"\b(?:bc1[a-z0-9]{20,}|0x[0-9a-fA-F]{40}|[48][0-9AB][1-9A-HJ-NP-Za-km-z]{90,}"
    r"|[13LM][a-km-zA-HJ-NP-Z1-9]{25,34})\b"
)
_CRYPTO_LOGO = re.compile(r"(?:xmr|btc|eth|ltc|monero|bitcoin|ether\w*|litecoin)\s*logo", re.I)
_DONATE_LINK = re.compile(
    r"^\s*(?:donate|donations?|tip|tip jar)(?:\s*/\s*(?:donate|tip))?\s*$", re.I
)


def _drop_donations(root: BeautifulSoup | Tag) -> None:
    """Saem os pedidos de donativos que os colaboradores põem no fim das páginas: o item com o
    nome da moeda e o endereço da carteira (e o logótipo dela) e as ligações «Donate/Tip». É
    ruído de pedido de dinheiro, não conteúdo, e o curador e o verificador tropeçavam nele
    («nomes perdidos: bitcoin, btc, logo, monero, xmr»). O nome do colaborador fica."""
    for item in list(root.find_all("li")):
        if item.decomposed or item.find("li") is not None:
            continue  # só os itens folha
        text = item.get_text(" ")
        if _CRYPTO_NAME.search(text) and _CRYPTO_ADDRESS.search(text):
            item.decompose()
    for img in list(root.find_all("img")):
        alt = img.get("alt")
        if not img.decomposed and isinstance(alt, str) and _CRYPTO_LOGO.search(alt):
            img.decompose()
    for link in list(root.find_all("a")):
        if link.decomposed or not _DONATE_LINK.match(link.get_text(" ")):
            continue
        parent = link.parent
        link.decompose()
        if isinstance(parent, Tag) and parent.name == "li" and not _has_content(parent):
            parent.decompose()


def _has_content(tag: Tag) -> bool:
    return any(s.strip() for s in tag.strings) or tag.find(_KEEPS_CONTENT) is not None


def _drop_empty_headings(root: BeautifulSoup | Tag) -> None:
    """Um título sem nada até ao título seguinte do mesmo nível (ou acima) sai («Referências»,
    «Ver também» vazios). Só se olha para dentro da raiz: o que vem depois (o rodapé) não conta."""
    nodes = list(root.descendants)
    doomed: list[Tag] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Tag) or node.name not in _HEADING_NAMES:
            continue
        level = int(node.name[1])
        empty = not node.get_text().strip()
        if not empty:
            empty = True
            for later in nodes[index + 1 :]:
                if isinstance(later, Tag):
                    if later.name in _HEADING_NAMES and int(later.name[1]) <= level:
                        break
                    if later.name in _KEEPS_CONTENT:
                        empty = False
                        break
                elif type(later) is NavigableString and str(later).strip():
                    if later.find_parent(_HEADING_NAMES) is None:
                        empty = False
                        break
        if empty:
            doomed.append(node)
    for heading in doomed:
        heading.decompose()


def _prune_empty(root: BeautifulSoup | Tag) -> None:
    """Os contentores que ficaram sem texto nem imagens (depois de tirar o lixo) saem."""
    for tag in reversed(root.find_all(_PRUNABLE)):
        if not tag.decomposed and not _has_content(tag):
            tag.decompose()


def _flatten_infoboxes(root: BeautifulSoup | Tag) -> None:
    """A caixa de informação (tabela com células aninhadas) vira uma lista «**Campo:** valor»."""
    for box in root.select("table.infobox"):
        items = _new("ul")
        for row in box.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            texts = [" ".join(c.get_text(" ").split()) for c in cells]
            texts = [t for t in texts if t]
            if len(texts) == 2:
                li = _new("li")
                bold = _new("b")
                bold.string = texts[0]
                li.append(bold)
                li.append(NavigableString(f": {texts[1]}"))
                items.append(li)
            elif len(texts) == 1:
                li = _new("li")
                li.string = texts[0]
                items.append(li)
        box.replace_with(items)


def prepare(
    html: str, *, boilerplate: Boilerplate | None = None
) -> tuple[BeautifulSoup | Tag, list[str]]:
    """A raiz limpa do artigo e o texto escondido que se tirou dela.

    Pela ordem: a raiz do conteúdo, o texto escondido, o título (o da página é o do Wiki.js), o
    lixo repetido da fonte (`boilerplate`), o refluxo dos blocos com `<br>`, a licença do ZIM, os
    pedidos de donativos, os títulos e contentores vazios, e a caixa de informação."""
    root = content_root(html)
    hidden = hidden_texts(root)
    for title in root.find_all("h1"):
        title.decompose()
    if boilerplate is not None and boilerplate.lines:
        _boilerplate.strip(root, boilerplate)
    _boilerplate.strip_banner(root)
    _boilerplate.strip_link_bars(root)
    _reflow(root)
    _drop_license(root)
    _drop_donations(root)
    _drop_empty_headings(root)
    _prune_empty(root)
    _flatten_infoboxes(root)
    return root, hidden


def hrefs(root: BeautifulSoup | Tag) -> list[str]:
    """Os `href` do artigo, sem repetições, pela ordem em que aparecem."""
    found = (a.get("href") for a in root.find_all("a"))
    return list(dict.fromkeys(h for h in found if isinstance(h, str)))


def md_url(url: str) -> str:
    """Um URL que cabe num link Markdown (parênteses e espaços em percent-encoding)."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


# --- imagens e legendas (secção 3) ------------------------------------------------------------

_CAPTIONS = "figcaption, .thumbcaption, .gallerytext"
_KEPT = "data-ariadne-kept"
_WRAPPERS = frozenset(
    {"a", "span", "font", "div", "center", "picture", "b", "i", "em", "strong", "u", "small", "p"}
)


def _img_src(img: Tag) -> str:
    """O `src` da imagem; nas carregadas à preguiça o verdadeiro vem em `data-src`."""
    src, lazy = img.get("src"), img.get("data-src")
    if (
        isinstance(lazy, str)
        and lazy.strip()
        and (not isinstance(src, str) or src.startswith("data:"))
    ):
        return lazy.strip()
    return src.strip() if isinstance(src, str) else ""


def _inside(node: Tag, ancestor: Tag) -> bool:
    return any(parent is ancestor for parent in node.parents)


def _figure_groups(root: BeautifulSoup | Tag) -> list[tuple[Tag, Tag, list[Tag]]]:
    """(contentor, legenda, imagens) de cada figura: o menor ancestral da legenda que tem imagens
    e só esta legenda. A legenda vem de `figcaption`, `.thumbcaption` ou `.gallerytext`."""
    groups: list[tuple[Tag, Tag, list[Tag]]] = []
    for caption in root.select(_CAPTIONS):
        node = caption.parent
        for _ in range(4):
            if not isinstance(node, Tag) or node is root:
                break
            if any(other is not caption for other in node.select(_CAPTIONS)):
                break
            pictures = [i for i in node.find_all("img") if not _inside(i, caption)]
            if pictures:
                groups.append((node, caption, pictures))
                break
            node = node.parent
    return groups


def images(root: BeautifulSoup | Tag) -> list[ImageRef]:
    """As imagens do artigo (sem repetir o `src`), com o texto alternativo e a legenda. Chama-se
    antes de `to_markdown`, que reescreve a árvore."""
    captions: dict[int, str] = {}
    caption_ids = {id(c) for c in root.select(_CAPTIONS)}
    for _container, caption, pictures in _figure_groups(root):
        text = _plain(caption)
        for picture in pictures:
            captions.setdefault(id(picture), text)
    found: dict[str, ImageRef] = {}
    for img in root.find_all("img"):
        src = _img_src(img)
        if not src or src in found or any(id(p) in caption_ids for p in img.parents):
            continue
        alt = img.get("alt")
        found[src] = ImageRef(src, _SPACES.sub(" ", alt).strip() if isinstance(alt, str) else "",
                              captions.get(id(img), ""))  # fmt: skip
    return list(found.values())


def _keep(img: Tag, url: str) -> Tag:
    for attr in ("data-src", "srcset", "title", "style", "class", "width", "height"):
        if attr in img.attrs:
            del img[attr]
    img["src"] = url
    img[_KEPT] = "1"
    return img


def _caption_paragraph(caption: Tag) -> Tag | None:
    """A legenda como parágrafo em itálico (sem blocos nem ícones lá dentro)."""
    for tag in caption.find_all("img"):
        tag.decompose()
    for tag in caption.find_all("br"):
        tag.replace_with(NavigableString(" "))
    for tag in caption.find_all(sorted(_BLOCK_TAGS)):
        tag.unwrap()
    if not caption.get_text().strip():
        return None
    emphasis = _new("em")
    for child in list(caption.contents):
        emphasis.append(child)
    _tidy(emphasis)
    return _new("p", emphasis)


def _standalone(img: Tag) -> Tag:
    """O que envolve só esta imagem (ligações, `div`s): se há texto ao lado, é a própria imagem."""
    unit = img
    while isinstance(unit.parent, Tag) and unit.parent.name in _WRAPPERS:
        parent = unit.parent
        if parent.get_text().strip() or len(parent.find_all("img")) != 1:
            break
        unit = parent
    return unit


def _drop_image(img: Tag) -> None:
    """Tira a imagem sem deixar dois espaços onde ela estava entre duas palavras."""
    before, after = img.previous_sibling, img.next_sibling
    img.decompose()
    if (
        type(before) is NavigableString
        and type(after) is NavigableString
        and str(before)[-1:].isspace()
        and str(after)[:1].isspace()
    ):
        after.replace_with(NavigableString(str(after).lstrip()))


def _place_images(root: BeautifulSoup | Tag, urls: Mapping[str, str]) -> None:
    """Fica `![alt](url)` (e a legenda em itálico por baixo) para as imagens enviadas ao Wiki.js;
    as outras saem. A legenda sai do sítio onde estava, para não ficar duplicada."""
    placed: set[int] = set()
    for container, caption, pictures in _figure_groups(root):
        position = 0
        for img in pictures:
            if _img_src(img) in urls:
                _keep(img, urls[_img_src(img)])
                container.insert(position, _new("p", img.extract()))
                placed.add(id(img))
                position += 1
        paragraph = _caption_paragraph(caption)
        if paragraph is not None:
            container.insert(position, paragraph)
        caption.decompose()
        for img in pictures:
            if id(img) not in placed:
                _drop_image(img)
    for img in list(root.find_all("img")):
        if img.decomposed or id(img) in placed:
            continue
        src = _img_src(img)
        if src not in urls:
            _drop_image(img)
            continue
        _keep(img, urls[src])
        unit = _standalone(img)
        if unit is not img:
            unit.replace_with(_new("p", img.extract()))


# --- avisos e código ---------------------------------------------------------------------------

_NOTE_SELECTOR = (
    ".admonition, .note, .warning, .caution, .important, .tip, .hint, .attention, .danger, "
    ".error, .ambox, .alert, .callout"
)
_NOTE_TAGS = frozenset({"div", "section", "p", "blockquote", "aside"})
_NOTE_KINDS = {
    "warning": "is-warning", "caution": "is-warning", "important": "is-warning",
    "attention": "is-warning", "ambox": "is-warning", "danger": "is-danger", "error": "is-danger",
    "tip": "is-success", "hint": "is-success",
}  # fmt: skip


def _admonitions(root: BeautifulSoup | Tag) -> None:
    """Os avisos e notas do HTML passam a citação com a classe do Wiki.js (`{.is-info}`)."""
    for box in root.select(_NOTE_SELECTOR):
        if (
            box.decomposed
            or box.name not in _NOTE_TAGS
            or box.find_parent(attrs={"data-admonition": True}) is not None
            or box.find_parent(["td", "th", "pre"]) is not None
        ):
            continue
        classes = [str(c).lower() for c in box.get("class") or []]
        kind = next((_NOTE_KINDS[c] for c in classes if c in _NOTE_KINDS), "is-info")
        box.name = "blockquote"
        box["data-admonition"] = kind
        for attr in ("class", "style", "id"):
            if attr in box.attrs:
                del box[attr]
        for title in box.select(".admonition-title"):
            title.name = "p"
            strong = _new("strong")
            for child in list(title.contents):
                strong.append(child)
            title.append(strong)


_LANG_SKIP = frozenset(
    {"default", "none", "text", "plain", "plaintext", "notranslate", "highlight", "hljs",
     "nohighlight", "code", "pre", "prettyprint", "sourcecode", "literal-block", "source",
     "highlighter-rouge", "linenums", "line-numbers"}
)  # fmt: skip
_LANG_PREFIX = re.compile(r"^(?:language|lang|highlight-source|highlight|brush:?)[-_:]?(.+)$")
_LANG_NAME = re.compile(r"^[a-z0-9][a-z0-9+#._-]{0,19}$")


def _class_language(classes: list[str]) -> str:
    tokens = [c.lower() for c in classes]
    for index, token in enumerate(tokens):
        name = ""
        if token in ("brush:", "brush", "sourcecode") and index + 1 < len(tokens):
            name = tokens[index + 1]
        elif match := _LANG_PREFIX.match(token):
            name = match.group(1)
        if name and name not in _LANG_SKIP and _LANG_NAME.match(name):
            return name
    return ""


def _code_language(pre: Tag) -> str:
    """A língua de um bloco de código, se a classe (do `pre`, do `code` que tem dentro, ou dos
    `div` que o rodeiam, como no Sphinx) a indicar; vazio se não."""
    candidates: list[Tag] = [pre]
    inner = pre.find("code")
    if isinstance(inner, Tag):
        candidates.append(inner)
    candidates.extend(p for p in list(pre.parents)[:3] if isinstance(p, Tag))
    for tag in candidates:
        for attr in ("data-lang", "data-language"):
            value = tag.get(attr)
            if isinstance(value, str) and _LANG_NAME.match(value.lower()):
                return value.lower()
        classes = tag.get("class")
        if isinstance(classes, list) and (found := _class_language(classes)):
            return found
    return ""


class _Converter(MarkdownConverter):
    def convert_img(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        if el.get(_KEPT) != "1":
            return ""
        alt = _SPACES.sub(" ", str(el.get("alt") or "")).strip()
        alt = alt.replace("[", "(").replace("]", ")").replace("|", "/")
        return f"![{alt}]({md_url(str(el.get('src') or ''))})"

    def convert_blockquote(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        quote: str = super().convert_blockquote(el, text, parent_tags)  # type: ignore[misc]
        kind = el.get("data-admonition")
        if isinstance(kind, str) and quote.strip():
            return f"{quote.rstrip()}\n{{.{kind}}}\n\n"
        return quote


_CODE = ("pre", "code", "kbd", "samp")


def _escape_text(root: BeautifulSoup | Tag) -> None:
    """O Wiki.js interpreta HTML cru no Markdown: `&`, `<` e `>` do texto passam a entidades (menos
    em código, onde o Markdown não interpreta nada) e os colchetes do texto de um link levam
    barra invertida. As quebras de linha do HTML (do código-fonte, sem significado) viram espaços,
    porque o Wiki.js trata cada quebra do texto como uma quebra de linha."""
    for node in list(root.find_all(string=True)):
        if type(node) is not NavigableString or node.find_parent(_CODE) is not None:
            continue
        text = str(node).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r"[ \t]*\n\s*", " ", text)
        if node.find_parent("a") is not None:
            text = text.replace("[", "\\[").replace("]", "\\]")
        node.replace_with(NavigableString(text))


def to_markdown(
    root: BeautifulSoup | Tag,
    targets: Mapping[str, str],
    images: Mapping[str, str] | None = None,
) -> str:
    """Markdown do artigo. `targets` diz para onde vai cada `href` interno; os externos mantêm-se
    e os restantes (âncoras, destinos que não são artigos) ficam só com o texto. `images` diz o URL
    no Wiki.js de cada `src` que foi enviado: essas ficam como `![alt](url)`, com a legenda em
    itálico por baixo, e as outras imagens saem."""
    _admonitions(root)
    _place_images(root, images or {})
    _escape_text(root)
    for anchor in list(root.find_all("a")):
        href = anchor.get("href")
        if "title" in anchor.attrs:
            del anchor["title"]  # repete o texto do link; no Markdown só atrapalha
        if isinstance(href, str) and href in targets:
            anchor["href"] = md_url(targets[href])
        elif isinstance(href, str) and _EXTERNAL.match(href):
            anchor["href"] = md_url(href)
        else:
            anchor.unwrap()
    markdown: str = _Converter(
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
        escape_underscores=False,
        code_language_callback=_code_language,
    ).convert_soup(root)
    markdown = _BLANK_SPACES.sub("", markdown)
    return _BLANK_LINES.sub("\n\n", _EDGE_RULES.sub("", markdown)).strip()


# --- a forma que ficou (secção 2.5) -----------------------------------------------------------

_MIN_CHARS = 3000
_MAX_MEAN_PARAGRAPH = 1500
_FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
_MD_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_MD_STRUCTURE = re.compile(r"^\s*(?:[|>]|[-*+]\s|\d+[.)]\s|!\[|\{\.)")


def format_quality(markdown: str) -> FormatQuality:
    """Se o Markdown ficou sem forma (mais de 3 mil caracteres, sem títulos e com parágrafos
    médios acima de 1500 caracteres), a página fica marcada para o lote LLM, que só pode mexer
    na forma."""
    text = _FENCE.sub("", markdown).strip()
    if len(text) <= _MIN_CHARS or _MD_HEADING.search(text):
        return FormatQuality(False)
    paragraphs = [
        p.strip()
        for p in re.split(r"\n\s*\n", text)
        if p.strip() and not all(_MD_STRUCTURE.match(line) for line in p.splitlines() if line)
    ]
    mean = sum(map(len, paragraphs)) // len(paragraphs) if paragraphs else 0
    if mean > _MAX_MEAN_PARAGRAPH:
        return FormatQuality(
            True, f"no headings and average paragraphs of {mean} characters in {len(text)}"
        )
    return FormatQuality(False)
