"""O título da página do Wiki.js (docs/WIKI-IMPORT.md, secção 2.7).

O `<title>` do ZIM é muitas vezes mau: o nome do site («Army Publishing Directorate»), um número de
série («PAPEL #7 TÉCNICO») ou o título com o site agarrado («… - MedlinePlus Medical Encyclopedia»,
«[OpenWrt Wiki] …», «LOW-TECH MAGAZINE: …»). Só regras determinísticas:

1. Os candidatos são o `<h1>`, o `og:title` e o `<title>` (o do ZIM), limpos do nome do site, que
   se aprende por fonte (`learn_names`): um prefixo ou sufixo, separado por `-`, `|`, `:`…, comum
   a mais de 30% das páginas amostradas (`Boilerplate.site`).
2. Um candidato é fraco se é o mesmo em muitas páginas da fonte (`Boilerplate.generic`: «Army
   Publishing Directorate», «LOW-TECH MAGAZINE»), se tem um número de série (`#7`) ou quase nada.
3. Vence o `<h1>`/`og:title` que concorda com o `<title>` (o `<h1>` de uma subsecção de um livro do
   wikibooks não substitui o título da página), ou o primeiro válido se o `<title>` é fraco. Se um
   candidato é o início de outro («Adalimumab» e «Adalimumab - Common brands: …») fica o mais curto.
4. Se nenhum serve, o 1.º cabeçalho do texto (já depois do refluxo: nas páginas do cd3wd as linhas
   em MAIÚSCULAS são cabeçalhos) e, em último caso, o título do ZIM.
5. Um título todo em MAIÚSCULAS passa a capitalização de frase.
"""

import html as htmllib
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from bs4 import BeautifulSoup
from bs4.element import Tag

MIN_COUNT = 3  # uma repetição em 3 páginas já é estrutura da fonte
FRACTION = 0.3  # e em mais de 30% da amostra
MIN_PAGES = 3  # com menos páginas nada se aprende
MAX_NAME_CHARS = 60
MAX_NAME_WORDS = 6
LABEL_WORDS = 3  # um rótulo em MAIÚSCULAS com até estas palavras é um tipo e não um título
FOLLOWING_WORDS = 4  # e o cabeçalho seguinte, com pelo menos estas palavras, é o título
SIGLA = 4  # letras de uma sigla antes de um número («TM 11-5825»)
AGREEMENT = 0.6  # fração das palavras do mais curto que o outro tem de conter

# Os separadores entre o título e o nome do site: « - », « | », «: », « :: », «•»…
_SEP = re.compile(r"\s+(?:[-–—|·»•]|:{2}|:)\s+|:\s+|\s*[|•»·]\s*")
_BRACKET_PREFIX = re.compile(r"^\[([^\]]{2,60})\]\s*(.+)$")
_INVISIBLE = {c for c in map(chr, range(0x200B, 0x2070)) if unicodedata.category(c) == "Cf"}
_TRAILING = re.compile(r"\s*(?:[¶§#]|\[(?:edit|editar|modifier)\])+\s*$", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_SERIAL = re.compile(r"#\s*\d")
_WORDS = re.compile(r"[^\W_]+")
_PLACEHOLDERS = frozenset(
    {"home", "index", "untitled", "main content", "loading", "loading…", "document", "new page"}
)


@dataclass(frozen=True)
class Raw:
    """Os candidatos do HTML, tal como estão."""

    h1: str = ""
    og: str = ""
    title: str = ""


@dataclass(frozen=True)
class Chosen:
    title: str
    origin: str  # h1, og, title, heading ou zim
    heading: Tag | None = None  # o cabeçalho do texto de onde veio (a página já o tem no título)


def tidy(text: str) -> str:
    """Espaços colapsados, sem marcas invisíveis (bidi) nem o «¶» dos cabeçalhos de documentação."""
    text = htmllib.unescape(text)
    text = "".join(c for c in text if c not in _INVISIBLE)
    text = _SPACES.sub(" ", text).strip()
    return _TRAILING.sub("", text).strip()


def shape(text: str) -> str:
    """O título sem maiúsculas nem números: «PAPEL #7 TÉCNICO» e «Papel #12 técnico» são iguais."""
    return _DIGITS.sub(
        "#", _SPACES.sub(" ", unicodedata.normalize("NFKC", text)).casefold()
    ).strip()


def page_titles(html: str) -> Raw:
    """O `<h1>` (o 1.º do documento), o `og:title` e o `<title>` de uma página."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    og = soup.find("meta", attrs={"property": "og:title"})
    title = soup.find("title")
    content = og.get("content") if isinstance(og, Tag) else None
    return Raw(
        tidy(h1.get_text(" ")) if isinstance(h1, Tag) else "",
        tidy(content) if isinstance(content, str) else "",
        tidy(title.get_text(" ")) if isinstance(title, Tag) else "",
    )


def _headings(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [tidy(h.get_text(" ")) for h in soup.find_all(["h1", "h2", "h3"])]


# --- aprender o nome do site ----------------------------------------------------------------


def _segments(title: str) -> set[str]:
    """Os pedaços de um título à volta dos separadores: o que vem antes e o que vem depois de
    cada um, e o texto entre parênteses retos à frente. Candidatos a nome do site."""
    found: set[str] = set()
    bracket = _BRACKET_PREFIX.match(title)
    if bracket:
        found.add(bracket.group(1))
    for sep in _SEP.finditer(title):
        found.add(title[: sep.start()])
        found.add(title[sep.end() :])
    names = {_SPACES.sub(" ", s).strip().casefold() for s in found}
    return {
        n
        for n in names
        if 2 <= len(n) <= MAX_NAME_CHARS
        and len(n.split()) <= MAX_NAME_WORDS
        and any(map(str.isalpha, n))
    }


def learn_names(pages: Sequence[Raw]) -> tuple[frozenset[str], frozenset[str]]:
    """(nomes do site, formas genéricas) a partir dos candidatos de uma amostra de páginas.

    O nome do site é um pedaço do `<title>` que está em mais de 30% das páginas (e em 3 pelo
    menos). Uma forma genérica é um título, `<h1>` ou `og:title` (já sem o nome do site) que se
    repete assim em muitas páginas, sem contar maiúsculas nem números."""
    if len(pages) < MIN_PAGES:
        return frozenset(), frozenset()
    limit = max(MIN_COUNT - 1, FRACTION * len(pages))
    segment_count: Counter[str] = Counter()
    for page in pages:
        segment_count.update(_segments(page.title))
    site = frozenset(n for n, count in segment_count.items() if count > limit)
    shape_count: Counter[str] = Counter()
    for page in pages:
        seen = {shape(clean(t, site)) for t in (page.title, page.h1, page.og) if t}
        shape_count.update(s for s in seen if s)
    generic = frozenset(s for s, count in shape_count.items() if count > limit)
    return site, generic


# --- escolher --------------------------------------------------------------------------------


def clean(raw: str, site: frozenset[str]) -> str:
    """O título sem o nome do site, à frente ou atrás, nem o «[Nome]» à frente."""
    title = tidy(raw)
    if not site or not title:
        return title
    for _ in range(3):  # o nome do site pode estar dos dois lados («Field Listing :: X — Site»)
        before = title
        bracket = _BRACKET_PREFIX.match(title)
        if bracket and bracket.group(1).strip().casefold() in site:
            title = bracket.group(2).strip()
        separators = list(_SEP.finditer(title))
        for sep in reversed(separators):
            tail = title[sep.end() :].strip().casefold()
            if tail in site or any(tail.startswith(n) for n in site if len(n) >= 8):
                title = title[: sep.start()].strip()
                break
        for sep in _SEP.finditer(title):
            if title[: sep.start()].strip().casefold() in site:
                title = title[sep.end() :].strip()
                break
        if title == before:
            break
    return title if len(title) >= 2 else tidy(raw)


def weak(title: str, generic: frozenset[str]) -> bool:
    """O título não identifica a página: quase vazio, de espaço reservado, com número de série ou
    igual em muitas páginas da fonte."""
    letters = sum(1 for c in title if c.isalpha())
    return (
        letters < 2
        or title.casefold().rstrip(".… ") in _PLACEHOLDERS
        or bool(_SERIAL.search(title))
        or shape(title) in generic
    )


def _tokens(text: str) -> set[str]:
    return {w.casefold() for w in _WORDS.findall(text)}


def agrees(a: str, b: str) -> bool:
    """Os dois títulos falam do mesmo: o mais curto tem 60% das palavras dentro do outro."""
    ta, tb = _tokens(a), _tokens(b)
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return bool(small) and len(small & large) >= AGREEMENT * len(small)


def _starts(short: str, long: str) -> bool:
    """`long` é `short` seguido de um separador e mais texto («Adalimumab - Common brands: …»)."""
    short, long = short.casefold().strip(), long.casefold().strip()
    if len(short) >= len(long) or not long.startswith(short):
        return False
    return _SEP.match(long[len(short) :]) is not None or long[len(short)] in " ,:"


def sentence_case(title: str) -> str:
    """Um título todo em MAIÚSCULAS passa a capitalização de frase. As palavras com números
    («A4», «T-1428») e os títulos curtos (siglas) ficam como estão."""
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 4 or not all(c.isupper() for c in letters) or len(title.split()) < 2:
        return title
    original = title.split(" ")
    words = [w if any(c.isdigit() for c in w) else w.lower() for w in original]
    for i, word in enumerate(original[:-1]):  # «TM 11-5825»: a sigla antes de um número fica
        if word.isalpha() and len(word) <= SIGLA and any(c.isdigit() for c in original[i + 1]):
            words[i] = word
    first = next((i for i, w in enumerate(words) if any(c.isalpha() for c in w)), None)
    if first is not None and words[first] == words[first].lower():
        words[first] = words[first][:1].upper() + words[first][1:]
    return " ".join(words)


def _is_label(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len(title.split()) <= LABEL_WORDS


class _Names(Protocol):
    """O que `choose` precisa da fonte (o `Boilerplate` cumpre-o)."""

    @property
    def site(self) -> frozenset[str]: ...

    @property
    def generic(self) -> frozenset[str]: ...


def choose(
    zim_title: str,
    html: str,
    boilerplate: _Names,
    *,
    root: Tag | BeautifulSoup | None = None,
) -> Chosen:
    """O título da página. `zim_title` é o do ZIM; `root` a árvore do texto já preparada (o 1.º
    cabeçalho dela é o recurso quando nenhum candidato serve)."""
    site, generic = boilerplate.site, boilerplate.generic
    raw = page_titles(html)
    title_text = clean(zim_title or raw.title, site)
    heading_texts = _headings(html)
    candidates = [
        ("h1", clean(raw.h1, site)),
        ("og", clean(raw.og, site)),
        ("title", title_text),
    ]
    valid = [(o, t) for o, t in candidates if t and not weak(t, generic)]
    base = next((t for o, t in valid if o == "title"), None)
    pool = [(o, t) for o, t in valid if o == "title" or base is None or agrees(t, base)]
    if pool:
        origin, best = pool[0]
        for other_origin, other in pool[1:]:
            if _starts(other, best):
                origin, best = other_origin, other
        if origin == "h1" and _is_label(best):
            follow = next(
                (
                    t
                    for t in heading_texts[1:3]
                    if len(t.split()) >= FOLLOWING_WORDS and not t.isupper()
                ),
                None,
            )
            if follow and not weak(clean(follow, site), generic):
                origin, best = "heading", clean(follow, site)
        return Chosen(sentence_case(best), origin)
    if root is not None:
        for heading in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            text = clean(heading.get_text(" "), site)
            if text and not weak(text, generic):
                return Chosen(sentence_case(text), "heading", heading)
    fallback = title_text or tidy(zim_title) or raw.h1 or raw.og
    return Chosen(sentence_case(fallback), "zim")
