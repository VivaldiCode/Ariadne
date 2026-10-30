"""Pesquisa de texto nos capítulos (nível 2 dos livros) e as citações por posição.

Os capítulos não estão no kiwix nem no Wiki.js: o texto extraído vive em `chapter.search_text` e a
coluna gerada `chapter.search_tsv` (Postgres, configuração `simple`, sem stemming, igual em qualquer
língua, com índice GIN) responde quando o nível 3 não chega ou não está disponível. Escolha: uma
tabela própria com `tsvector` em vez do índice do Kavita, porque o Kavita só pesquisa metadados e
nomes de séries, não o texto dos capítulos, e porque aqui o texto é exatamente o que `read`
devolve, o que dá posições de citação exatas. Procura primeiro os capítulos com **todos** os termos;
se faltar para `k`, completa com os que têm **algum**, pela ordem do `ts_rank_cd`. Os capítulos
rejeitados na revisão humana não aparecem.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from ariadne.courses.search import query_terms

# Âncoras de busca da passagem, da mais específica à mais curta: a primeira que se encontra vale.
# Uma só frase curta repete-se num livro; 200 caracteres quase nunca.
NEEDLE_SIZES = (200, 100, 60, 30)
# Marcas do excerto no `ts_headline`: caracteres de controlo, que o texto dos capítulos nunca traz
# (a extração tira-os), por isso não se confundem com o conteúdo.
START, STOP = "\x01", "\x02"


@dataclass(frozen=True)
class ChapterHit:
    source_id: str
    path: str
    title: str
    snippet: str
    rank: float
    position: int  # deslocamento no texto do capítulo (o que `read` devolve)


_SQL = """
WITH q AS (SELECT to_tsquery('simple', :tsquery) AS query),
hits AS (
  SELECT c.document_id, c.title, c.search_text,
         ts_rank_cd(c.search_tsv, q.query) AS rank
    FROM chapter c JOIN document d ON d.id = c.document_id, q
   WHERE d.source_id = ANY(:sources)
     AND c.search_tsv @@ q.query
     AND NOT EXISTS (SELECT 1 FROM curation cu
                      WHERE cu.document_id = c.document_id AND cu.decision = 'rejected')
   ORDER BY rank DESC, c.document_id
   LIMIT :k
)
SELECT d.source_id, d.path, h.title, h.rank,
       ts_headline('simple', h.search_text, q.query,
                   'MaxFragments=1, MinWords=12, MaxWords=40, '
                   'StartSel=\x01, StopSel=\x02') AS snippet,
       h.document_id
  FROM hits h JOIN document d ON d.id = h.document_id, q
 ORDER BY h.rank DESC, d.id
"""


def clean_snippet(snippet: str) -> str:
    return re.sub(r"\s+", " ", snippet.replace(START, "").replace(STOP, "")).strip()


def locate(session: Session, document_id: str, needle: str, terms: Sequence[str] = ()) -> int:
    """Onde, no texto do capítulo, está `needle` (deslocamento a contar de 0). Procura a âncora
    inteira até 200 caracteres e, se o texto não a tem tal e qual, versões mais curtas; sem nada,
    o primeiro dos `terms`; 0 se nem isso."""
    needle = needle.strip()
    tried: set[str] = set()
    for size in NEEDLE_SIZES:
        anchor = needle[:size]
        if len(anchor) < 8 or anchor in tried:
            continue
        tried.add(anchor)
        found = session.scalar(
            text("SELECT strpos(search_text, :n) FROM chapter WHERE document_id = :id"),
            {"n": anchor, "id": document_id},
        )
        if found:
            return int(found) - 1
    for term in terms:
        found = session.scalar(
            text(
                "SELECT strpos(lower(search_text), lower(:n)) FROM chapter WHERE document_id = :id"
            ),
            {"n": term, "id": document_id},
        )
        if found:
            return int(found) - 1
    return 0


def search_chapters(
    session: Session, query: str, source_ids: Sequence[str], k: int
) -> list[ChapterHit]:
    terms = query_terms(query)
    if not terms or not source_ids:
        return []
    found: dict[tuple[str, str], ChapterHit] = {}
    modes = ["&"] if len(terms) == 1 else ["&", "|"]
    for operator in modes:
        if len(found) >= k:
            break
        rows = session.execute(
            text(_SQL),
            {"tsquery": f" {operator} ".join(terms), "sources": list(source_ids), "k": k},
        ).all()
        for source_id, path, title, rank, snippet, document_id in rows:
            if (source_id, path) in found:
                continue
            raw = snippet.replace(START, "").replace(STOP, "")
            found[(source_id, path)] = ChapterHit(
                source_id,
                path,
                title,
                clean_snippet(snippet),
                float(rank),
                locate(session, document_id, raw, terms),
            )
    return list(found.values())[:k]
