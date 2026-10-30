"""Do Markdown dos blocos ao HTML: páginas do Moodle, enunciados e o texto que se pesquisa.

O canónico guarda Markdown com HTML inline limitado (é o que o freeCodeCamp escreve). Ao desenhar,
o Markdown passa pelo markdown-it com HTML ligado e o resultado por uma lista de permissões: o que
não está nela sai (`<script>`, `<iframe>`, `on*`, `style`, `javascript:`…), as imagens viram o seu
texto alternativo e as ligações só ficam com `http(s)`/`mailto`. A mesma função serve o Moodle e o
leitor (`CourseSource`), por isso nada perigoso chega a nenhum dos dois.
"""

import html as htmllib
import re
from functools import cache

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag
from markdown_it import MarkdownIt

from ariadne.courses.schema import (
    CodeExampleBlock,
    Course,
    ExerciseBlock,
    Lesson,
    Module,
    QuizBlock,
    SolutionBlock,
    TextBlock,
    VideoBlock,
    WorkedExampleBlock,
)

ALLOWED_TAGS = frozenset(
    [
        "p",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "blockquote",
        "pre",
        "code",
        "em",
        "strong",
        "i",
        "b",
        "u",
        "s",
        "sub",
        "sup",
        "kbd",
        "samp",
        "var",
        "mark",
        "small",
        "del",
        "ins",
        "abbr",
        "cite",
        "dfn",
        "q",
        "span",
        "div",
        "a",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "figure",
        "figcaption",
        "details",
        "summary",
    ]
)
DROP_WITH_CONTENT = frozenset(
    [
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "form",
        "input",
        "button",
        "select",
        "textarea",
        "svg",
        "math",
        "audio",
        "video",
        "link",
        "meta",
        "head",
        "title",
        "noscript",
        "template",
    ]
)
ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "code": frozenset({"class"}),
    "th": frozenset({"colspan", "rowspan"}),
    "td": frozenset({"colspan", "rowspan"}),
    "abbr": frozenset({"title"}),
}
SAFE_SCHEMES = ("http://", "https://", "mailto:", "#")
VOID_TAGS = frozenset({"br", "hr"})
_CODE_CLASS = re.compile(r"^language-[a-z0-9_+-]{1,30}$")
_DIGITS = re.compile(r"^[0-9]{1,3}$")
_INVISIBLE = re.compile(r"[\x00-\x20\x7f-\x9f\u200b-\u200f\u2028\u2029\ufeff]")


@cache
def _markdown() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table")


def _safe_href(href: object) -> str | None:
    """O destino de uma ligação se for seguro; os espaços e controlos que o navegador ignora dentro
    do esquema (`java\tscript:`) tiram-se antes de o comparar."""
    if not isinstance(href, str):
        return None
    compact = _INVISIBLE.sub("", htmllib.unescape(href)).lower()
    return href.strip() if compact.startswith(SAFE_SCHEMES) else None


def _attributes(tag: Tag) -> str | None:
    """Os atributos permitidos, escapados; `None` se a etiqueta sai (ligação insegura)."""
    out: list[str] = []
    allowed = ALLOWED_ATTRS.get(tag.name, frozenset())
    for name, value in tag.attrs.items():
        if name not in allowed:
            continue
        text = " ".join(value) if isinstance(value, list) else str(value)
        if name == "href":
            safe = _safe_href(text)
            if safe is None:
                return None
            text = safe
        elif (name == "class" and not _CODE_CLASS.match(text)) or (
            name in ("colspan", "rowspan") and not _DIGITS.match(text)
        ):
            continue
        out.append(f' {name}="{htmllib.escape(text, quote=True)}"')
    if tag.name == "a" and not any(a.startswith(" href=") for a in out):
        return None
    return "".join(out)


def _emit(node: PageElement, out: list[str]) -> None:
    """Reconstrói o HTML elemento a elemento: só sai o que a lista de permissões deixa, com o
    texto e os valores sempre escapados. Comentários, CDATA, declarações e instruções de
    processamento (o que o analisador guarda tal e qual e re-serializaria) não saem."""
    if isinstance(node, Tag):
        if node.name in DROP_WITH_CONTENT:
            return
        if node.name == "img":
            alt = node.get("alt")
            if isinstance(alt, str) and alt.strip():
                out.append(htmllib.escape(f"[imagem: {alt}]", quote=False))
            return
        if node.name not in ALLOWED_TAGS:
            for child in node.children:
                _emit(child, out)
            return
        attributes = _attributes(node)
        if attributes is None:  # ligação sem destino seguro: fica o texto
            for child in node.children:
                _emit(child, out)
            return
        if node.name in VOID_TAGS:
            out.append(f"<{node.name}>")
            return
        out.append(f"<{node.name}{attributes}>")
        for child in node.children:
            _emit(child, out)
        out.append(f"</{node.name}>")
    elif type(node) is NavigableString:  # exclui Comment, CData, Doctype, Script, …
        out.append(htmllib.escape(str(node), quote=False))


def sanitize_html(fragment: str) -> str:
    """O fragmento HTML só com o que a lista de permissões deixa (ver `_emit`)."""
    soup = BeautifulSoup(fragment, "html.parser")
    out: list[str] = []
    for child in soup.children:
        _emit(child, out)
    return "".join(out)


def markdown_to_html(markdown: str) -> str:
    return sanitize_html(_markdown().render(markdown))


def code_html(code: str, language: str = "") -> str:
    css = f' class="language-{htmllib.escape(language, quote=True)}"' if language else ""
    return f"<pre><code{css}>{htmllib.escape(code)}</code></pre>"


def _heading(text: str, level: int = 2) -> str:
    return f"<h{level}>{htmllib.escape(text)}</h{level}>"


def block_html(block: object, *, with_exercise: bool = True, with_solution: bool = False) -> str:
    """O HTML de um bloco. A solução só se desenha se for pedida (nunca para os alunos)."""
    if isinstance(block, TextBlock):
        return markdown_to_html(block.markdown)
    if isinstance(block, CodeExampleBlock):
        caption = f"<p><em>{htmllib.escape(block.caption)}</em></p>" if block.caption else ""
        return caption + code_html(block.code, block.language)
    if isinstance(block, WorkedExampleBlock):
        return (
            _heading("Exemplo resolvido", 3)
            + markdown_to_html(block.statement)
            + code_html(block.code, block.language)
            + (markdown_to_html(block.explanation) if block.explanation else "")
        )
    if isinstance(block, ExerciseBlock):
        if not with_exercise:
            return ""
        parts = [_heading("Exercício", 2)]
        if block.statement:
            parts.append(markdown_to_html(block.statement))
        if block.starter_code:
            parts.append(code_html(block.starter_code, block.language))
        if block.tests:
            items = "".join(f"<li>{htmllib.escape(t.description)}</li>" for t in block.tests)
            parts.append(f"<p>O código tem de passar:</p><ul>{items}</ul>")
        return "".join(parts)
    if isinstance(block, QuizBlock):
        parts = [_heading("Quiz", 2)]
        for question in block.questions:
            options = "".join(f"<li>{htmllib.escape(c.text)}</li>" for c in question.choices)
            parts.append(markdown_to_html(question.prompt) + f"<ol>{options}</ol>")
        return "".join(parts)
    if isinstance(block, SolutionBlock):
        if not with_solution:
            return ""
        return (
            _heading("Solução", 2)
            + code_html(block.code, block.language)
            + (markdown_to_html(block.explanation) if block.explanation else "")
        )
    if isinstance(block, VideoBlock):
        link = (
            f'<p><a href="{htmllib.escape(block.url, quote=True)}">'
            f"{htmllib.escape(block.title)}</a></p>"
            if block.url
            else f"<p>{htmllib.escape(block.title)}</p>"
        )
        transcript = markdown_to_html(block.transcript) if block.transcript else ""
        return (
            _heading("Vídeo", 2)
            + link
            + (_heading("Transcrição", 3) + transcript if transcript else "")
        )
    return ""


def lesson_html(lesson: Lesson, *, with_solution: bool = False, with_exercise: bool = True) -> str:
    return "".join(
        block_html(block, with_exercise=with_exercise, with_solution=with_solution)
        for block in lesson.blocks
    )


def text_html_before_exercise(lesson: Lesson) -> str:
    """O enunciado de uma lição com exercício: o texto, os exemplos e o vídeo antes do exercício."""
    parts: list[str] = []
    for block in lesson.blocks:
        if isinstance(block, ExerciseBlock | SolutionBlock | QuizBlock):
            continue  # o exercício e o quiz são perguntas do Moodle, não texto
        parts.append(block_html(block))
    return "".join(parts)


def overview_html(course: Course) -> str:
    """A página de apresentação do curso (também o texto do documento «índice» do curso)."""
    parts = [markdown_to_html(course.summary) if course.summary else ""]
    if course.objectives:
        items = "".join(f"<li>{htmllib.escape(o)}</li>" for o in course.objectives)
        parts.append(_heading("Objetivos", 2) + f"<ul>{items}</ul>")
    parts.append(_heading("Conteúdo", 2))
    for module in course.modules:
        lessons = "".join(f"<li>{htmllib.escape(lesson.title)}</li>" for lesson in module.lessons)
        parts.append(f"{_heading(module.title, 3)}<ul>{lessons}</ul>")
    parts.append(f"<p><small>{htmllib.escape(course.license.attribution)}</small></p>")
    return "".join(parts)


def module_summary_html(module: Module) -> str:
    return markdown_to_html(module.summary) if module.summary else ""
