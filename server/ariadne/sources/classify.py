"""Classificação à entrada (STRUCTURE.md, secção 4): artigo, curso ou livro.

Cada fonte (ou secção de uma fonte) é classificada para seguir para a base certa:

| Classe | Critério | Destino |
|---|---|---|
| `article` | referência sem percurso de aprendizagem (enciclopédias, Q&A, documentação) | Wiki.js |
| `course` | tem percurso (módulos e lições) e, normalmente, exercícios ou testes | Moodle |
| `book` | obra com capítulos, lida de forma contínua, sem exercícios | Calibre / Kavita |

Regra de fronteira para os casos intermédios (Wikibooks, LibreTexts, livros técnicos):
**com exercícios → curso; só referência → livro**. Aqui a decisão faz-se pelos metadados do ZIM
(nome, criador, etiquetas), que dizem quase sempre o que é; a regra de fronteira aplica-se a quem
não consegue decidir por eles com `has_exercises` (um extrator que olha para o conteúdo) e pelas
correções manuais, `SOURCE_CLASS_OVERRIDES` («zim:wikibooks_en_all=book,zim:x/Python=course», onde
o que vem depois de «/» é o prefixo de caminho da secção).

Uma fonte `course` **com extrator** (hoje o freeCodeCamp: `meta.extractable`) fica como arquivo
(`archive_only`): o que se pesquisa e se lê são as lições do curso extraído, não as páginas do ZIM.
Uma fonte `course` sem extrator continua pesquisável como artigos: esconder o que ainda não se
sabe extrair seria perdê-lo.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class ContentClass(StrEnum):
    ARTICLE = "article"
    COURSE = "course"
    BOOK = "book"

    @property
    def content_type(self) -> str:
        """O `document.content_type` do que esta classe produz."""
        return {"article": "article", "course": "lesson", "book": "chapter"}[self.value]


# Criadores de ZIM que publicam cursos com exercícios.
COURSE_CREATORS = ("freecodecamp", "khan academy", "codecademy")
# Prefixos do nome do ZIM por classe.
COURSE_PREFIXES = ("freecodecamp", "khanacademy", "khan_academy", "wikiversity")
BOOK_PREFIXES = ("wikibooks", "gutenberg", "libretexts", "wikisource", "openstax")
# Etiquetas do ZIM que sugerem cursos.
COURSE_TAGS = ("fcc", "course", "courses", "curso", "cursos")


@dataclass(frozen=True)
class Classification:
    content_class: ContentClass
    reason: str


def classify_zim(
    *, name: str, creator: str = "", tags: str = "", description: str = ""
) -> Classification:
    """Classe de um ZIM pelos seus metadados (sem abrir o conteúdo)."""
    lowered = name.casefold()
    creator_key = creator.casefold().strip()
    if any(creator_key.startswith(c) for c in COURSE_CREATORS):
        return Classification(ContentClass.COURSE, f"creator {creator!r}: courses with exercises")
    if lowered.startswith(COURSE_PREFIXES):
        return Classification(ContentClass.COURSE, "course project")
    tag_set = {t.strip().casefold() for t in tags.replace(",", ";").split(";") if t.strip()}
    if tag_set & set(COURSE_TAGS) and not lowered.startswith(BOOK_PREFIXES):
        return Classification(ContentClass.COURSE, "course tags")
    if lowered.startswith(BOOK_PREFIXES):
        return Classification(ContentClass.BOOK, "books and manuals (reference only by default)")
    return Classification(ContentClass.ARTICLE, "reference with no learning path")


def has_exercises(text: str) -> bool:
    """Regra de fronteira: o texto traz exercícios corrigíveis (testes, soluções)? Serve a
    extratores que classificam por secção (livros técnicos, LibreTexts)."""
    lowered = text.casefold()
    markers = ("# --hints--", "exercise", "exercício", "solution", "solução", "quiz", "assert")
    return sum(marker in lowered for marker in markers) >= 2


def parse_overrides(csv: str) -> dict[str, ContentClass]:
    """«zim:a=book, zim:b/Python=course» -> {"zim:a": BOOK, "zim:b/Python": COURSE}."""
    found: dict[str, ContentClass] = {}
    for item in csv.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            continue
        try:
            found[key.strip()] = ContentClass(value.strip().casefold())
        except ValueError as exc:
            raise ValueError(
                f"SOURCE_CLASS_OVERRIDES: unknown class {value!r} in {item!r} "
                "(article, course or book)"
            ) from exc
    return found


def effective_class(
    source_id: str,
    default: ContentClass,
    overrides: Mapping[str, ContentClass],
    path: str | None = None,
) -> ContentClass:
    """A classe de uma fonte, ou de uma secção dela (o prefixo de caminho mais longo ganha)."""
    best: tuple[int, ContentClass] | None = None
    for key, value in overrides.items():
        base, _, prefix = key.partition("/")
        if base != source_id:
            continue
        if prefix and (path is None or not path.startswith(prefix)):
            continue
        if best is None or len(prefix) > best[0]:
            best = (len(prefix), value)
    return best[1] if best else default
