"""A **obra** de um livro entre línguas, e a regra de entrada (ARIAD-32, decisão de 28/10/2026).

O dono quer livros em todas as línguas, mas «apenas livros que não foram originalmente escritos
naquela língua»: entram os **originais** de todas as línguas e, além deles, as **traduções para
línguas onde a obra ainda não existe**. A mesma obra nunca se repete na mesma língua.

**Ranhura.** Cada livro ocupa a ranhura `(obra, língua)`. A regra é só esta:

- ranhura livre: o livro entra (original, tradução ou sem prova de nada);
- ranhura ocupada: um **original provado** entra na mesma e a tradução que lá estava fica marcada
  como substituída (não se apaga nada sozinho: o relatório diz qual remover com `books remove`);
  tudo o resto é recusado (outra edição da mesma obra, ou tradução para uma língua que já a tem).

**Quem é a obra** (a chave), da fonte mais fiável para a menos, só com o que existe offline:

1. `wd:<QID>`, o item do Wikidata da obra. É a única identidade que atravessa línguas (a mesma obra
   tem o mesmo QID em `pt`, `en` e `fr`) e já está no projeto: as importações de *sitelinks*
   (ARCHITECTURE.md, secção 8) ligam cada artigo de Wikipédia/Wikisource ao seu `concept`. O QID vem
   do identificador do próprio livro (`wikidata:Q…`) ou do conceito do artigo cujo título é o do
   livro, na língua do livro, **se for um só**; em ambos os casos `concept.origin_lang` diz a língua
   original. **Hoje não há conceitos na base** (a importação do dump do Wikidata ainda não correu),
   por isso esta camada está pronta mas dormente.
2. `ta:<autor>|<título>`, o autor (nome sem acentos, datas nem ordem) e o título normalizado
   (`title_signature`: sem acentos, artigos nem título alternativo «; Or, …»). Vem dos metadados do
   EPUB, que o Gutenberg preenche e que é igual em todas as línguas para o autor. Apanha as edições
   repetidas na mesma língua e as traduções que conservam o título; **não** liga «Os Lusíadas» a
   «The Lusiads». Isso só o Wikidata faz, por isso sem ele a regra é conservadora: na dúvida o
   livro entra.
3. Sem autor (ou «Anónimo», «Various»), ou sem língua, **não há chave** e o livro entra sempre:
   um título comum sem autor misturaria obras diferentes e recusaria livros a mais.

A alternativa de comparar vetores multilingues do título e do autor (já temos o `multilingual-e5`)
ligaria «Os Lusíadas» a «The Lusiads», mas é uma probabilidade, não uma identidade: precisa de um
limiar calibrado com livros reais, e recusar por engano perde um livro. Fica para depois.

**Original ou tradução** (a ordem de quem decide): um tradutor nos metadados (`dc:contributor` com
`opf:role="trl"`) é uma tradução; senão, com QID e `concept.origin_lang`, é original se a língua do
livro é a de origem; senão **não se sabe** e o livro conta como `desconhecido`.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ariadne.books.extract import EpubCredits
from ariadne.db import models
from ariadne.textutil import title_key
from ariadne.wiki.language import to_iso1

TRANSLATION, UNKNOWN, ORIGINAL = 0, 1, 2
RANK_NAMES = {TRANSLATION: "translation", UNKNOWN: "unknown", ORIGINAL: "original"}


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_ARTICLES = _words(
    "the a an o os as um uma uns umas le la les l un une des el los las lo una der die das ein "
    "eine il gli"
)
# «Moby Dick; Or, The Whale»: o título alternativo do Gutenberg (só estes marcadores, nunca um «ou»
# solto, que faz parte de títulos como «Guerra ou Paz»).
_ALTERNATIVE = re.compile(r"\s*[;,]\s*(?:or|ou|oder|o|ó|ovvero)\s*,\s+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_WORDS = re.compile(r"[a-z]+")
_PARENTHESES = re.compile(r"[(\[].*?[)\]]")
_QID = re.compile(r"\bQ[1-9]\d*\b")
_ANONYMOUS = _words(
    "anonymous anonymus anonym anonyme anonimo anonim unknown inconnu inconnue desconhecido "
    "desconhecida various varios vários diversos divers verschiedene autori"
)


def _plain(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", value).casefold())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def title_signature(title: str) -> str:
    """O título para comparar edições: minúsculas, sem acentos, pontuação, artigo inicial nem
    título alternativo. Os volumes contam («Tomo I» e «Tomo II» são livros diferentes)."""
    first = _ALTERNATIVE.split(unicodedata.normalize("NFKC", title).casefold(), maxsplit=1)[0]
    words = _NON_ALNUM.sub(" ", _plain(first)).split()
    if len(words) > 1 and words[0] in _ARTICLES:
        words = words[1:]
    signature = " ".join(words)
    # Um título em cirílico, grego ou CJK reduz-se a números («Том 1»): não identifica nada e
    # juntaria obras diferentes do mesmo autor. Sem letras latinas que cheguem, o livro entra.
    return signature if sum(c.isalpha() for c in signature) >= 2 else ""


def author_signature(authors: list[str] | tuple[str, ...]) -> str:
    """O 1.º autor sem acentos, datas nem ordem («Verne, Jules, 1828-1905» e «Jules Verne» dão o
    mesmo). Vazio se não há autor ou é um anónimo/coletivo."""
    for author in authors:
        words = _WORDS.findall(_plain(_PARENTHESES.sub(" ", author)))
        words = [w for w in words if len(w) > 1]
        if not words:
            continue
        if _ANONYMOUS.intersection(words):
            return ""
        return " ".join(sorted(set(words)))
    return ""


@dataclass(frozen=True)
class WorkMeta:
    """O que se sabe do livro antes de ele entrar na biblioteca."""

    title: str
    authors: tuple[str, ...] = ()
    translators: tuple[str, ...] = ()
    language: str | None = None  # ISO 639-1
    identifiers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_credits(cls, credits: EpubCredits, fallback_language: str | None) -> "WorkMeta":
        return cls(
            title=credits.title,
            authors=credits.authors,
            translators=credits.translators,
            language=language_code(credits.language) or fallback_language,
            identifiers=dict(credits.identifiers),
        )


@dataclass(frozen=True)
class Work:
    """A obra de um livro e o lugar dele na regra."""

    key: str
    basis: str  # "wikidata" ou "autor+título"
    language: str  # ISO 639-1: a língua do livro
    rank: int  # ORIGINAL, UNKNOWN ou TRANSLATION
    origin_lang: str | None = None

    def as_meta(self) -> dict[str, Any]:
        """O que fica em `book.meta["work"]` (e se lê ao carregar o registo)."""
        return {
            "key": self.key,
            "basis": self.basis,
            "language": self.language,
            "rank": self.rank,
            "origin_lang": self.origin_lang,
        }


def language_code(code: str | None) -> str | None:
    """A língua do livro: ISO 639-1 se tem, senão o código de 3 letras (`ceb`); None sem língua."""
    if not code or "," in code:
        return None
    base = re.split(r"[-_]", code.strip().lower(), maxsplit=1)[0]
    return to_iso1(base) or (base if re.fullmatch(r"[a-z]{2,3}", base) else None)


def _explicit_qid(identifiers: dict[str, str]) -> str | None:
    if _QID.fullmatch(identifiers.get("wikidata", "")):
        return identifiers["wikidata"]
    for value in identifiers.values():
        if "wikidata" in value.casefold() and (found := _QID.search(value)):
            return found.group(0)
    return None


def _concept_qid(session: Session, title: str, language: str) -> str | None:
    """O QID do conceito de um artigo de Wikipédia/Wikisource que tem o título do livro, na língua
    do livro. Só vale se for um só (dois QID diferentes = título ambíguo: não se adivinha)."""
    rows = session.execute(
        text(
            "SELECT DISTINCT c.wikidata_qid FROM document d JOIN concept c ON c.id = d.concept_id "
            "WHERE d.language = :language AND d.title_keys && CAST(:keys AS text[]) "
            "AND c.wikidata_qid IS NOT NULL LIMIT 2"
        ),
        {"language": language, "keys": [title_key(title)]},
    ).all()
    return rows[0][0] if len(rows) == 1 else None


def identify(session: Session, meta: WorkMeta) -> Work | None:
    """A obra do livro, ou `None` se não há como a identificar (o livro entra sempre)."""
    language = meta.language
    if not language or not meta.title.strip():
        return None
    qid = _explicit_qid(meta.identifiers) or _concept_qid(session, meta.title, language)
    origin: str | None = None
    if qid is not None:
        key, basis = f"wd:{qid}", "wikidata"
        origin = session.scalar(
            select(models.Concept.origin_lang).where(models.Concept.wikidata_qid == qid)
        )
    else:
        author, title = author_signature(meta.authors), title_signature(meta.title)
        if not author or not title:
            return None
        key, basis = f"ta:{author}|{title}", "autor+título"
    if meta.translators:
        rank = TRANSLATION
    elif origin:
        rank = ORIGINAL if origin == language else TRANSLATION
    else:
        rank = UNKNOWN
    return Work(key, basis, language, rank, origin)


@dataclass(frozen=True)
class Holder:
    book_id: str
    rank: int


@dataclass(frozen=True)
class Decision:
    kind: Literal["admit", "refuse", "supersede"]
    holder: Holder | None = None
    reason: str = ""


class WorkRegistry:
    """As ranhuras ocupadas `(obra, língua)` pelos livros ativos. Carrega-se uma vez por corrida e
    atualiza-se à medida que os livros entram (a regra vale dentro da própria corrida)."""

    def __init__(self) -> None:
        self.slots: dict[tuple[str, str], Holder] = {}

    @classmethod
    def load(cls, session: Session) -> "WorkRegistry":
        """Os livros que já têm `meta.work` usam-no; os de antes da regra (os 600 do Gutenberg PT)
        ganham a chave de autor+título, sem prova de original nem de tradução."""
        registry = cls()
        rows = session.execute(
            select(
                models.Book.id,
                models.Book.title,
                models.Book.authors,
                models.Book.language,
                models.Book.meta,
            ).where(models.Book.status == "active")
        )
        for book_id, title, authors, language, meta in rows:
            stored = meta.get("work") if isinstance(meta, dict) else None
            if isinstance(stored, dict) and stored.get("key") and stored.get("language"):
                key, lang, rank = (
                    stored["key"],
                    stored["language"],
                    int(stored.get("rank", UNKNOWN)),
                )
            else:
                lang = language_code(language)
                author, signature = author_signature(authors or []), title_signature(title or "")
                if not lang or not author or not signature:
                    continue
                key, rank = f"ta:{author}|{signature}", UNKNOWN
            current = registry.slots.get((key, lang))
            if current is None or rank > current.rank:
                registry.slots[(key, lang)] = Holder(book_id, rank)
        return registry

    def decide(self, work: Work | None) -> Decision:
        if work is None:
            return Decision("admit")
        holder = self.slots.get((work.key, work.language))
        if holder is None:
            return Decision("admit")
        language = work.language
        if work.rank == ORIGINAL and holder.rank < ORIGINAL:
            return Decision(
                "supersede",
                holder,
                f"original in {language}: the edition {holder.book_id!r} "
                f"({RANK_NAMES[holder.rank]}) is now redundant",
            )
        if work.rank == TRANSLATION:
            reason = (
                f"translation into {language}, and the work already exists in that language "
                f"({holder.book_id})"
            )
        else:
            reason = f"another edition of the same work in {language} ({holder.book_id})"
        return Decision("refuse", holder, reason)

    def record(self, work: Work | None, book_id: str) -> None:
        if work is None:
            return
        slot = (work.key, work.language)
        current = self.slots.get(slot)
        if current is None or work.rank >= current.rank:
            self.slots[slot] = Holder(book_id, work.rank)
