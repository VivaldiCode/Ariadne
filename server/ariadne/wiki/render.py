"""A página do Wiki.js: cabeçalho com a proveniência, o corpo curado, o bloco «Ligações»
(ARCHITECTURE.md, secção 7.4) e a atribuição de licença da origem."""

import hashlib
import html
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from ariadne.kiwix import content_url
from ariadne.wiki.convert import md_url

# Tipo de ligação -> título do grupo, pela ordem em que aparecem na página.
KIND_LABELS = {
    "langlink": "Outras línguas",
    "wikidata": "Mesmo conceito",
    "title_match": "Mesmo título noutras fontes",
    "manual": "Ligadas à mão",
    "vector": "Assuntos semelhantes",
}
ORIGINAL_SEAL = "fonte original"
_WIKIMEDIA = re.compile(
    r"^(wikipedia|wikibooks|wikiversity|wikisource|wikinews|wikivoyage|wiktionary|wikiquote|"
    r"wikispecies|wikimed|mdwiki)(?:[_-]|$)"
)
_DOMAIN = {"wikimed": "wikipedia"}
_CC_BY_SA_4 = ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/")
SUMMARY_CHARS = 240


def escape_md(text: str) -> str:
    """Texto de fora (títulos, nomes de fontes) que cabe numa linha de Markdown sem virar HTML,
    link ou código."""
    for char, repl in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(char, repl)
    for char in ("\\", "[", "]", "`", "*", "_", "|"):
        text = text.replace(char, "\\" + char)
    return " ".join(text.split())


def escape_code(text: str) -> str:
    """Texto para dentro de um `code span`: sem crases nem quebras de linha."""
    return " ".join(text.replace("`", "'").split())


@dataclass(frozen=True)
class License:
    name: str | None  # None: desconhecida
    url: str | None
    origin_url: str | None  # a página original na Wikimedia, quando se sabe


def license_for(source_id: str, zim_name: str | None, language: str | None, title: str) -> License:
    """A licença de origem a partir do projeto da fonte: os projetos Wikimedia são CC BY-SA 4.0;
    uma fonte desconhecida fica «ver fonte», sem inventar nada."""
    name = (zim_name or source_id.split(":", 1)[-1]).casefold()
    match = _WIKIMEDIA.match(name)
    if match is None:
        return License(None, None, None)
    project = _DOMAIN.get(match.group(1), match.group(1))
    origin = None
    if language and re.fullmatch(r"[a-z]{2,3}", language) and project != "mdwiki":
        origin = f"https://{language}.{project}.org/wiki/{quote(title.replace(' ', '_'))}"
    return License(*_CC_BY_SA_4, origin)


@dataclass(frozen=True)
class LinkEntry:
    kind: str
    source_title: str
    source_language: str | None
    title: str
    url: str | None  # a página do Wiki.js se existe, senão o kiwix, senão nada
    score: float | None = None
    original: bool = False  # a versão na língua original do conceito: o selo «fonte original»


def links_block(entries: Sequence[LinkEntry]) -> str:
    """«Ligações» agrupado por tipo e, dentro dele, por fonte. Vazio se não há ligações."""
    if not entries:
        return ""
    by_kind: dict[str, dict[tuple[str, str | None], list[LinkEntry]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for entry in entries:
        by_kind[entry.kind][(entry.source_title, entry.source_language)].append(entry)
    lines = ["## Ligações", ""]
    for kind in [*KIND_LABELS, *sorted(set(by_kind) - set(KIND_LABELS))]:
        if kind not in by_kind:
            continue
        lines += [f"### {KIND_LABELS.get(kind, kind)}", ""]
        groups = by_kind[kind].items()  # a fonte original primeiro, depois por nome
        for (source_title, language), group in sorted(
            groups,
            key=lambda item: (not any(e.original for e in item[1]), item[0][0], item[0][1] or ""),
        ):
            label = escape_md(f"{source_title} ({language})" if language else source_title)
            lines += [f"**{label}**", ""]
            for entry in sorted(group, key=lambda e: (-(e.score or 0), e.title)):
                text = escape_md(entry.title)
                link = f"[{text}]({md_url(entry.url)})" if entry.url else text
                lines.append(f"- {link}" + (f" · **{ORIGINAL_SEAL}**" if entry.original else ""))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def header(
    *,
    source_title: str,
    source_id: str,
    version: str,
    path: str,
    kiwix_url: str | None,
    curation: str,
    archived: bool = False,
    withdrawn: bool = False,
) -> str:
    """Proveniência no topo: de onde vem, que versão, que caminho, e como foi tratado."""
    source_title = escape_md(source_title)
    origin = f"[{source_title}]({md_url(kiwix_url)})" if kiwix_url else source_title
    lines = []
    if withdrawn:
        lines.append(
            "> **Retirado:** o texto desta página foi retirado numa revisão humana e não "
            "está disponível."
        )
        lines.append(">")
    if archived:
        lines.append(
            "> **Arquivado:** este artigo já não existe na versão atual da fonte; "
            "fica aqui como estava na última versão que o tinha."
        )
        lines.append(">")
    lines.append(
        f"> **Fonte:** {origin} · `{escape_code(source_id)}` · versão {escape_md(version)}"
        f" · caminho `{escape_code(path)}`  \n> **Tratamento:** {escape_md(curation)}"
    )
    return "\n".join(lines)


def footer(licence: License, source_title: str) -> str:
    source_title = escape_md(source_title)
    if licence.name is None:
        text = f"Licença do conteúdo: ver fonte ({source_title})."
    else:
        text = f"Conteúdo de {source_title}, licenciado sob [{licence.name}]({licence.url})"
        if licence.origin_url:
            text += f"; artigo original em <{licence.origin_url}>, com o histórico dos autores"
        text += ". Esta página é uma versão tratada: alterações podem ter sido feitas."
    return f"---\n\n*{text}*"


def assemble(head: str, body: str, links: str, foot: str) -> str:
    return "\n\n".join(part.strip() for part in (head, body, links, foot) if part.strip()) + "\n"


_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_NAV_SPLIT = re.compile(r"\s+[-–—•·|/»›]\s+|\s*[•·|»›]\s*")
PROSE_CHARS = 80  # um parágrafo de texto corrido tem pelo menos isto, frases e poucos números
PROSE_WORDS = 12
PROSE_DIGITS = 0.08


def _plain(block: str) -> str:
    text = _MD_LINK.sub(r"\1", block.strip())
    text = re.sub(r"[*_`]", "", html.unescape(text))
    return " ".join(text.split())


def is_navigation(block: str) -> bool:
    """O parágrafo é uma barra de navegação e não texto: quase só ligações, ou uma fila de
    palavras curtas separadas por `-`, `|`, `•` («Home - English - French - German»)."""
    links = _MD_LINK.findall(block)
    plain = _plain(block)
    if len(links) >= 2 and sum(len(" ".join(t.split())) for t in links) >= 0.7 * len(plain):
        return True
    parts = [p for p in _NAV_SPLIT.split(plain) if p.strip()]
    return len(parts) >= 4 and all(len(p.split()) <= 3 for p in parts)


def _prose(text: str) -> bool:
    """Texto corrido: frases, palavras, poucos dígitos (e não uma morada, uma ficha ou um nome)."""
    digits = sum(1 for c in text if c.isdigit())
    return (
        len(text) >= PROSE_CHARS
        and len(text.split()) >= PROSE_WORDS
        and bool(re.search(r"[.!?…]($|\s)", text))
        and digits <= PROSE_DIGITS * len(text)
    )


def summary(markdown: str) -> str:
    """O 1.º parágrafo de texto do corpo (sem títulos, listas, citações nem barras de navegação),
    para a descrição. Prefere texto corrido (frases, palavras, poucos números); só se não houver,
    qualquer parágrafo com 40 caracteres."""
    candidates: list[str] = []
    for block in re.split(r"\n\s*\n", markdown):
        head = block.strip()
        if head.startswith(("#", "-", ">", "|", "```")):
            continue
        if is_navigation(block):
            continue
        text = _plain(block)
        if len(text) >= 40:
            candidates.append(text)
    chosen = next((t for t in candidates if _prose(t)), candidates[0] if candidates else "")
    if len(chosen) <= SUMMARY_CHARS:
        return chosen
    cut = chosen.rfind(" ", 0, SUMMARY_CHARS)
    return chosen[: cut if cut > SUMMARY_CHARS // 2 else SUMMARY_CHARS].rstrip(",;:.") + " …"


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def kiwix_link(public_url: str, book: object, path: str) -> str | None:
    return content_url(public_url, str(book), path) if book else None


# --- páginas de domínio (docs/WIKI-IMPORT.md, secção 6) --------------------------------------

DOMAIN_TAG = "dominio"  # a etiqueta das páginas de domínio (as dos artigos levam locale e domínio)
INDEX_LIMIT = 500  # artigos (e livros) listados numa página de domínio; o resto vem em «e mais N»
_INDEX_LABELS = {
    "pt": {
        "parent": "Dentro de",
        "children": "Subdomínios",
        "articles": "Artigos",
        "books": "Livros",
        "book": "Livro",
        "more": "… e mais {n}.",
        "empty": "Ainda não há páginas neste domínio.",
        "note": "Página gerada a partir da árvore de domínios.",
    },
    "en": {
        "parent": "Inside",
        "children": "Subdomains",
        "articles": "Articles",
        "books": "Books",
        "book": "Book",
        "more": "… and {n} more.",
        "empty": "There are no pages in this domain yet.",
        "note": "Page generated from the domain tree.",
    },
}


@dataclass(frozen=True)
class IndexEntry:
    """Uma linha de uma página de domínio: o título e para onde vai (`None`: só o texto)."""

    title: str
    url: str | None = None


def _index_line(entry: IndexEntry, suffix: str = "") -> str:
    text = escape_md(entry.title)
    link = f"[{text}]({md_url(entry.url)})" if entry.url else text
    return f"- {link}{suffix}"


def domain_index(
    *,
    locale: str,
    description: str,
    parent: IndexEntry | None,
    children: Sequence[IndexEntry],
    articles: Sequence[IndexEntry],
    books: Sequence[IndexEntry],
    more_articles: int = 0,
    more_books: int = 0,
) -> str:
    """O Markdown da página de um domínio: a descrição, o domínio de cima, os subdomínios, os
    artigos e os livros (estes com link para o kiwix). Determinístico: a ordem é a recebida
    (quem chama ordena por título) e nada depende da hora."""
    labels = _INDEX_LABELS.get(locale, _INDEX_LABELS["en"])
    parts = [description.strip()] if description.strip() else []
    if parent is not None:
        parts.append(f"*{labels['parent']}: {_index_line(parent)[2:]}*")
    if children:
        parts.append("\n".join([f"## {labels['children']}", "", *map(_index_line, children)]))
    if articles:
        lines = [f"## {labels['articles']}", "", *map(_index_line, articles)]
        if more_articles:
            lines += ["", labels["more"].format(n=more_articles)]
        parts.append("\n".join(lines))
    if books:
        lines = [
            f"## {labels['books']}",
            "",
            *(_index_line(b, f" · *{labels['book']}*") for b in books),
        ]
        if more_books:
            lines += ["", labels["more"].format(n=more_books)]
        parts.append("\n".join(lines))
    if not (children or articles or books):
        parts.append(labels["empty"])
    parts.append(f"---\n\n*{labels['note']}*")
    return "\n\n".join(parts) + "\n"
