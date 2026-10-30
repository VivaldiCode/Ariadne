"""Pesquisa de texto nas lições (nível 2 dos cursos).

As lições não estão no kiwix: o índice `lesson.search_text` (Postgres, configuração `simple`,
sem stemming, igual em qualquer língua) responde quando o nível 3 não chega ou não está
disponível. Procura primeiro as lições com **todos** os termos; se faltar para `k`, completa com as
que têm **algum**, pela ordem do `ts_rank_cd`.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

_WORD = re.compile(r"\w{2,}", re.UNICODE)
MAX_TERMS = 12
# Palavras que não distinguem uma pergunta (PT, EN, ES): fora da pesquisa de texto.
STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "how",
        "what",
        "which",
        "who",
        "why",
        "when",
        "where",
        "does",
        "can",
        "are",
        "was",
        "were",
        "this",
        "that",
        "these",
        "those",
        "from",
        "into",
        "about",
        "have",
        "has",
        "you",
        "your",
        "out",
        "not",
        "but",
        "any",
        "all",
        "como",
        "que",
        "qual",
        "quais",
        "quem",
        "para",
        "por",
        "com",
        "sem",
        "uma",
        "uns",
        "umas",
        "dos",
        "das",
        "nos",
        "nas",
        "num",
        "numa",
        "isto",
        "isso",
        "mais",
        "menos",
        "sobre",
        "entre",
        "desde",
        "ate",
        "são",
        "foi",
        "ser",
        "tem",
        "ter",
        "muito",
        "una",
        "unos",
        "unas",
        "los",
        "las",
        "del",
        "con",
        "cómo",
    }
)


@dataclass(frozen=True)
class LessonHit:
    source_id: str
    path: str
    title: str
    snippet: str
    rank: float


def query_terms(query: str) -> list[str]:
    seen: list[str] = []
    for word in _WORD.findall(query.casefold()):
        if word not in STOPWORDS and word not in seen and len(word) > 2:
            seen.append(word)
    return seen[:MAX_TERMS]


_SQL = """
SELECT d.source_id, d.path, l.title, l.rank,
       ts_headline('simple', l.search_text, to_tsquery('simple', :tsquery),
                   'MaxFragments=1, MinWords=12, MaxWords=40, StartSel=⟦, StopSel=⟧') AS snippet
  FROM (
    SELECT l.document_id, l.title, l.search_text,
           ts_rank_cd(to_tsvector('simple', l.search_text), to_tsquery('simple', :tsquery)) AS rank
      FROM lesson l JOIN document d ON d.id = l.document_id
     WHERE d.source_id = ANY(:sources)
       AND to_tsvector('simple', l.search_text) @@ to_tsquery('simple', :tsquery)
     ORDER BY rank DESC, l.document_id
     LIMIT :k
  ) l JOIN document d ON d.id = l.document_id
 ORDER BY l.rank DESC, d.id
"""


def _clean(snippet: str) -> str:
    return re.sub(r"\s+", " ", snippet.replace("⟦", "").replace("⟧", "")).strip()


def search_lessons(
    session: Session, query: str, source_ids: Sequence[str], k: int
) -> list[LessonHit]:
    terms = query_terms(query)
    if not terms or not source_ids:
        return []
    found: dict[tuple[str, str], LessonHit] = {}
    modes = ["&"] if len(terms) == 1 else ["&", "|"]
    for operator in modes:
        if len(found) >= k:
            break
        rows = session.execute(
            text(_SQL),
            {"tsquery": f" {operator} ".join(terms), "sources": list(source_ids), "k": k},
        ).all()
        for source_id, path, title, rank, snippet in rows:
            found.setdefault(
                (source_id, path),
                LessonHit(
                    source_id, path, title, re.sub(r"\s+", " ", snippet).strip(), float(rank)
                ),
            )
    return list(found.values())[:k]
