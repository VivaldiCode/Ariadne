"""A regra histórica do router (ARCHITECTURE.md, secções 5 e 8): em temas de história, a versão na
língua original do conceito é muitas vezes a mais rica e fiável, por isso acrescenta-se aos
resultados (ou, se já lá está, assinala-se) com o selo `original_source`.

Só age se `ROUTER_HISTORICAL_RULE` está ligada, a pergunta é histórica (`router_rules.classify`)
e se conhece a língua da pergunta. Depois, para os primeiros resultados de um conceito com
`origin_lang` conhecida e diferente da língua da pergunta:

- se o resultado já está nessa língua, fica marcado como fonte original;
- senão, a versão do conceito nessa língua entra logo a seguir, marcada. Respeita a pesquisa:
  só vêm fontes dentro das **pedidas** (filtros `lang` e `sources`), nunca uma versão ligada por
  uma aresta `langlink`/`wikidata` rejeitada, nem um item arquivado ou rejeitado na curadoria; e o
  total nunca passa de `k` (a versão original ocupa o lugar do último resultado, exceto o 1.º).

Nada se inventa: sem `origin_lang` ou sem a versão, não se faz nada.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ariadne.db.models import Concept, Curation, Link
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Source as SourceRow
from ariadne.documents import read_article
from ariadne.router import Candidate
from ariadne.router_rules import classify, detect_language
from ariadne.services import Services
from ariadne.sources.base import Source, SourceError
from ariadne.textutil import clean_text

logger = logging.getLogger(__name__)

TOP = 3  # só os primeiros resultados disparam a regra
MIN_PARAGRAPH = 40
SNIPPET_CHARS = 300
LANGUAGE_KINDS = ("langlink", "wikidata")


@dataclass(frozen=True)
class HistoricalOutcome:
    candidates: list[Candidate]
    reasons: tuple[str, ...] = ()  # porque a pergunta é histórica (vazio: a regra não se aplicou)
    added: int = 0
    marked: int = 0


def apply(
    services: Services,
    session: Session,
    query: str,
    query_lang: str | None,
    ranked: list[Candidate],
    chosen: Sequence[Source],
    k: int,
) -> HistoricalOutcome:
    """Aplica a regra aos resultados (`chosen` são as fontes pedidas, `k` o máximo). Nunca
    levanta por causa da regra: sem dados, devolve-os como vieram."""
    if not ranked or not services.settings.router_historical_rule:
        return HistoricalOutcome(ranked)
    verdict = classify(query)
    if not verdict.historical:
        return HistoricalOutcome(ranked)
    language = query_lang or detect_language(query)
    if language is None:
        return HistoricalOutcome(ranked, verdict.reasons)

    allowed = {source.id for source in chosen}
    ids = [f"{c.source.id}/{c.path}" for c in ranked]
    rows = session.execute(
        select(DocumentRow.id, DocumentRow.language, Concept.id, Concept.origin_lang)
        .join(Concept, Concept.id == DocumentRow.concept_id)
        .where(DocumentRow.id.in_(ids), Concept.origin_lang.is_not(None))
    ).all()
    concept_of: dict[str, tuple[int, str]] = {}
    own_language: dict[str, str | None] = {}
    for document_id, document_language, concept_id, origin in rows:
        own_language[document_id] = document_language
        if origin is not None and origin != language:
            concept_of[document_id] = (concept_id, origin)

    result: list[Candidate] = []
    present = {c.key for c in ranked}
    handled: set[int] = set()
    added_keys: list[tuple[str, str]] = []
    marked = 0
    for position, candidate in enumerate(ranked):
        document_id = f"{candidate.source.id}/{candidate.path}"
        found = concept_of.get(document_id)
        if found is None:
            result.append(candidate)
            continue
        concept_id, origin = found
        if own_language.get(document_id) == origin:  # já é a versão original
            result.append(replace(candidate, original=True))
            handled.add(concept_id)
            marked += 1
            continue
        result.append(candidate)
        if position >= TOP or concept_id in handled:
            continue
        handled.add(concept_id)
        original = _original_version(
            services, session, concept_id, origin, document_id, candidate, present, allowed
        )
        if original is not None:
            result.append(original)
            present.add(original.key)
            added_keys.append(original.key)
    result = _fit(result, added_keys, k)
    added = sum(1 for c in result if c.key in added_keys)
    if added == 0 and marked == 0:
        return HistoricalOutcome(ranked, verdict.reasons)
    return HistoricalOutcome(result, verdict.reasons, added, marked)


def _fit(result: list[Candidate], added: list[tuple[str, str]], k: int) -> list[Candidate]:
    """Mantém o total em `k`: sai o último resultado que não é a fonte original (nunca o 1.º); se
    não há, saem as versões acrescentadas, da última para a primeira."""
    result = list(result)
    pending = list(added)
    while len(result) > k:
        removable = [i for i, c in enumerate(result) if i > 0 and not c.original]
        if removable:
            result.pop(removable[-1])
        elif pending:
            key = pending.pop()
            result = [c for c in result if c.key != key]
        else:
            result.pop()
    return result


def _original_version(
    services: Services,
    session: Session,
    concept_id: int,
    origin: str,
    trigger_id: str,
    trigger: Candidate,
    present: set[tuple[str, str]],
    allowed: set[str],
) -> Candidate | None:
    """A versão do conceito na língua original: de uma fonte ativa **pedida** que o registo
    conhece, sem aresta rejeitada com o resultado e sem curadoria arquivada ou rejeitada."""
    rows = session.execute(
        select(DocumentRow, Curation)
        .join(SourceRow, SourceRow.id == DocumentRow.source_id)
        .outerjoin(Curation, Curation.document_id == DocumentRow.id)
        .where(
            DocumentRow.concept_id == concept_id,
            DocumentRow.language == origin,
            SourceRow.status == "active",
            DocumentRow.source_id.in_(sorted(allowed)),
        )
    ).all()
    rejected = _rejected_with(session, trigger_id)
    project = trigger.source.meta.get("project")
    options = []
    for document, curation in rows:
        if document.id in rejected:
            continue
        if curation is not None and (
            curation.archived_at is not None or curation.decision == "rejected"
        ):
            continue
        source = services.registry.get(document.source_id)
        if source is None or (source.id, document.path) in present:
            continue
        same_project = bool(project) and source.meta.get("project") == project
        options.append((not same_project, source.id, document, curation, source))
    if not options:
        return None
    _, _, document, curation, source = min(options, key=lambda o: (o[0], o[1]))
    # Nível 2 só com uma página válida no Wiki.js: existe, e a curadoria não a arquivou nem retirou.
    has_page = document.wiki_page_id is not None and curation is not None
    return Candidate(
        source=source,
        path=document.path,
        title=document.title,
        snippet=_snippet(source, document.path),
        level=2 if has_page else 1,
        score=trigger.score,
        original=True,
    )


def _rejected_with(session: Session, document_id: str) -> set[str]:
    """Os documentos ligados a este por uma aresta de língua rejeitada na curadoria."""
    rows = session.execute(
        select(Link.src_document, Link.dst_document).where(
            Link.kind.in_(LANGUAGE_KINDS),
            Link.status == "rejected",
            or_(Link.src_document == document_id, Link.dst_document == document_id),
        )
    )
    return {other for pair in rows for other in pair if other != document_id}


def _snippet(source: Source, path: str) -> str:
    """O 1.º parágrafo com substância do artigo (a versão original não passou por uma pesquisa)."""
    try:
        article = read_article(source, path)
    except SourceError:
        return ""
    for block in article.blocks:
        if block.kind == "p" and len(block.text) >= MIN_PARAGRAPH:
            return clean_text(block.text)[:SNIPPET_CHARS]
    return ""
