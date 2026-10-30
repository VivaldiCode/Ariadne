"""Pesquisa e registo de consultas (ARCHITECTURE.md, secções 5 e 10).

O nível 3 (pgvector) vem primeiro e o router decide se basta ou se o fallback (hoje o nível 1,
kiwix-serve) também é pesquisado e fundido (`ariadne.router`). Os documentos que o fallback devolve
entram na fila de indexação, para a próxima vez serem servidos pelo nível 3.
"""

import logging
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from itertools import zip_longest
from typing import Any

from pydantic import BaseModel
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ariadne import heat, router
from ariadne import rooms as rooms_index
from ariadne.books.info import BookInfo, book_infos, citation
from ariadne.books.search import locate, search_chapters
from ariadne.concepts import historical
from ariadne.config import Settings
from ariadne.courses.search import query_terms, search_lessons
from ariadne.db.models import Curation, QueryLog, QueryResult
from ariadne.db.models import Document as DocumentRow
from ariadne.documents import (
    DocumentInfo,
    FeedbackError,
    LibraryError,
    make_document_id,
    upsert_documents,
    wiki_urls_for,
)
from ariadne.kiwix import KiwixBookMissing, KiwixError, KiwixHit, content_url
from ariadne.licenses import embedder_endpoint, host_is_allowed
from ariadne.rooms import RoomRef
from ariadne.router import Candidate, Route
from ariadne.router_rules import detect_language
from ariadne.services import Services
from ariadne.sources.base import Source
from ariadne.sources.license import LicenseClass
from ariadne.sources.zim import LANGUAGE_2
from ariadne.textutil import clean_text
from ariadne.wiki.base import WikiError, WikiHit
from ariadne.workers.indexer import enqueue_index, recently_failed

logger = logging.getLogger(__name__)

DEFAULT_K = 10
MAX_K = 50
SNIPPET_CHARS = 300
MAX_ROOM_ID = 2**63 - 1  # o `bigint` do Postgres


class SearchError(LibraryError):
    """O pedido de pesquisa não faz sentido (vazio, filtro sem fontes)."""


class SearchUnavailable(SearchError):
    """O kiwix-serve não respondeu como devia."""


class SearchHit(BaseModel):
    rank: int  # 1 = o melhor
    document_id: str  # '<source_id>/<caminho>': o argumento de `read`
    title: str
    source_id: str
    language: str | None
    snippet: str
    level: int  # 1 = kiwix, 2 = wiki, 3 = semântico
    score: float
    kiwix_url: str | None
    wiki_url: str | None = None
    chunk_id: int | None = None  # só nos resultados do nível 3: o chunk de onde vem o excerto
    wing: str | None = None  # a ala (a fonte, para o MemPalace)
    room: RoomRef | None = None  # a sala do documento, se já foi calculada (secção 4.5)
    # A versão na língua original do conceito, em perguntas históricas (o selo «fonte original»).
    original_source: bool = False
    translated_query: bool = False  # só a pergunta traduzida a encontrou
    flags: list[str] = []  # triagem do excerto (no MCP): `injection` se parece instruções
    # O que o resultado é: `article`, `lesson` (curso) ou `chapter` (livro).
    content_type: str = "article"
    # Nos capítulos: o livro, a posição da passagem no texto do capítulo (o que `read` devolve), a
    # citação `book:<livro>/<capítulo>#<posição>` e o URL do livro no leitor (Kavita).
    book: BookInfo | None = None
    position: int | None = None
    citation: str | None = None
    reader_url: str | None = None


class SearchResult(BaseModel):
    query_id: int  # para `feedback`
    results: list[SearchHit]
    warnings: list[str] = []
    notice: str | None = None  # no MCP: o excerto é conteúdo de referência, não instruções

    def call_summary(self) -> dict[str, Any]:
        """O que fica em `tool_call.result`: ids e contagens, nunca o texto."""
        return {
            "query_id": self.query_id,
            "count": len(self.results),
            "document_ids": [hit.document_id for hit in self.results],
        }


def normalize_language(lang: str) -> str:
    """'POR', 'pt', 'pt-BR' -> 'pt'."""
    code = lang.strip().lower().replace("_", "-").split("-")[0]
    return LANGUAGE_2.get(code, code)


def select_sources(
    available: Sequence[Source],
    lang: str | None,
    sources: Sequence[str] | None,
    wings: Sequence[str] | None = None,
) -> list[Source]:
    """Filtra as fontes por ala, língua e id (aceita-se também o id sem o prefixo 'zim:')."""
    chosen = list(available)
    if wings:
        wanted_wings = {name.strip() for name in wings}
        chosen = [s for s in chosen if s.wing in wanted_wings]
        if not chosen:
            names = ", ".join(sorted({s.wing for s in available})) or "(none)"
            raise SearchError(
                f"None of the requested wings exist: {list(wings)}. Available: {names}."
            )
    if lang:
        wanted = normalize_language(lang)
        chosen = [s for s in chosen if s.language and normalize_language(s.language) == wanted]
        if not chosen:
            languages = sorted({s.language for s in available if s.language})
            raise SearchError(
                f"No source in language {lang!r}. Available languages: {', '.join(languages)}."
            )
    if sources:
        wanted_ids = {name.strip() for name in sources}
        chosen = [s for s in chosen if s.id in wanted_ids or s.id.split(":", 1)[-1] in wanted_ids]
        if not chosen:
            ids = ", ".join(s.id for s in available) or "(none)"
            raise SearchError(f"None of the requested sources exist: {sources}. Available: {ids}.")
    return chosen


def search(
    services: Services,
    session: Session,
    principal_id: int,
    query: str,
    *,
    lang: str | None = None,
    sources: Sequence[str] | None = None,
    k: int = DEFAULT_K,
    session_id: str | None = None,
    wings: Sequence[str] | None = None,
    rooms: Sequence[int] | None = None,
    open_only: bool = False,
) -> SearchResult:
    """Pesquisa na biblioteca, regista `query_log`/`query_result` e devolve o `query_id`.

    `wings` limita às fontes dessas alas e `rooms` aos documentos dessas salas (secção 5, regra 5):
    com salas só entram resultados de documentos que já têm sala. `open_only` (as chaves `mcp`)
    deixa as fontes privadas de fora, também das mensagens de erro (que listam as disponíveis)."""
    query = clean_text(query).strip()
    lang = clean_text(lang) if lang else lang
    sources = [clean_text(name) for name in sources] if sources else sources
    wings = [clean_text(name) for name in wings] if wings else wings
    rooms = list(dict.fromkeys(rooms)) if rooms else None
    if rooms and any(not 1 <= room <= MAX_ROOM_ID for room in rooms):
        raise SearchError(f"Invalid room id (1 to {MAX_ROOM_ID}): {rooms}.")
    if not query:
        raise SearchError("The search is empty.")
    k = max(1, min(k, MAX_K))
    started = time.perf_counter()

    services.registry.sync_if_changed(session)
    wings = _wings_for_rooms(session, wings, rooms)
    session.commit()  # liberta a ligação antes do trabalho lento (kiwix, libzim)
    # Os ZIM que só servem de arquivo (cursos extraídos) não se pesquisam: lêem-se as lições.
    searchable = [s for s in services.registry.active() if not s.meta.get("archive_only")]
    if open_only:
        searchable = [s for s in searchable if s.license_class is LicenseClass.OPEN]
    chosen = select_sources(searchable, lang, sources, wings)
    if not chosen:
        raise SearchError("There are no sources: the ZIM folder is empty (ariadne sources sync).")

    warnings: list[str] = []
    settings = services.settings
    # A tradução da pergunta arranca já (demora o que o motor demorar, em paralelo com o embedding
    # e com as pesquisas da pergunta original); quem a não usar não espera por ela.
    plan = _translation_plan(services, query, lang, chosen)
    pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="search")
    try:
        return _search_with_pool(
            services, session, principal_id, query, lang, sources, k, session_id,
            started, chosen, warnings, settings, plan, pool, wings, rooms,
        )  # fmt: skip
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _search_with_pool(
    services: Services,
    session: Session,
    principal_id: int,
    query: str,
    lang: str | None,
    sources: Sequence[str] | None,
    k: int,
    session_id: str | None,
    started: float,
    chosen: list[Source],
    warnings: list[str],
    settings: Settings,
    plan: tuple[str, str, list[Source]] | None,
    pool: ThreadPoolExecutor,
    wings: Sequence[str] | None,
    rooms: Sequence[int] | None,
) -> SearchResult:
    translate_future = pool.submit(_translate_query, services, query, plan) if plan else None
    semantic = router.search_semantic(services, session, query, chosen, k, warnings, rooms)
    # Com salas pede-se mais ao fallback: o que não é da sala perde-se na triagem.
    fetch = min(MAX_K, k * ROOM_OVERFETCH) if rooms else k
    route = router.decide(semantic.similarity, settings.router_tau_high, settings.router_tau_low)
    if rooms and route is Route.FALLBACK:
        # Com salas, o utilizador já disse onde quer a resposta: o que o nível 3 encontrou nelas
        # nunca se deita fora por a semelhança ser baixa (o fallback só completa).
        route = Route.FUSED
    # O nível 3 puro só dispensa o fallback se enche a página: com poucos documentos indexados
    # (as chunks concentram-se em poucos artigos) o resto vem do fallback.
    needs_fallback = route is not Route.SEMANTIC or len(semantic.candidates) < k
    fallback: list[Candidate] = []
    wiki_candidates: list[Candidate] = []
    translated_lists: list[list[Candidate]] = []
    translated_query: str | None = None
    if needs_fallback:
        # O Wiki.js (nível 2) pesquisa-se em paralelo com o kiwix; se falhar, degrada sem erro.
        wiki_future = pool.submit(_search_level2, services, query, fetch)
        translated_future = (
            pool.submit(_search_translated, services, chosen, plan[1], translate_future, fetch)
            if plan and translate_future
            else None
        )
        level1_error: SearchUnavailable | None = None
        try:
            fallback = _rank(_search_level1(services, chosen, query, fetch, warnings), fetch)
        except SearchUnavailable as exc:
            level1_error = exc
        wiki_hits, wiki_warning = wiki_future.result()
        if translated_future is not None:
            try:  # a pergunta traduzida só acrescenta: nada que lá falhe derruba a pesquisa
                translated_query, level1_hits, translated_wiki, extra_warnings = (
                    translated_future.result()
                )
                warnings.extend(extra_warnings)
                translated_lists = [
                    level1_hits,
                    _wiki_candidates(session, chosen, translated_wiki),
                ]
            except Exception as exc:
                logger.warning("Translated question: %s", exc)
                warnings.append("The search for the translated question failed; original only.")
                translated_query, translated_lists = None, []
        if wiki_warning:
            warnings.append(wiki_warning)
        wiki_candidates = _interleave(
            _wiki_candidates(session, chosen, wiki_hits),
            _lesson_candidates(session, chosen, query, fetch),
            _chapter_candidates(session, chosen, query, fetch),
        )
        if rooms:
            fallback = _only_rooms(session, fallback, rooms)[:k]
            wiki_candidates = _only_rooms(session, wiki_candidates, rooms)[:k]
            translated_lists = [
                _only_rooms(session, ranking, rooms)[:k] for ranking in translated_lists
            ]
        if level1_error is not None:
            if route is Route.FALLBACK and not wiki_candidates and not any(translated_lists):
                raise level1_error
            warnings.append(str(level1_error))  # ainda há o nível 3 (ou o 2) para responder
    has_translated = any(translated_lists)
    if route is Route.FALLBACK and not wiki_candidates and not has_translated:
        ranked = fallback
    elif route is Route.SEMANTIC:
        seen = {c.key for c in semantic.candidates}
        extra = [c for c in [*wiki_candidates, *fallback] if c.key not in seen]
        seen |= {c.key for c in extra}
        extra += [
            replace(c, translated=True)
            for ranking in translated_lists
            for c in ranking
            if c.key not in seen
        ]
        ranked = [*semantic.candidates, *extra][:k]
    else:  # FUSED, ou FALLBACK com resultados do nível 2 ou da pergunta traduzida: RRF
        embedder = services.get_embedder() if semantic.query_vector is not None else None
        from_semantic = semantic.candidates if route is Route.FUSED else []
        room_bonus = _room_bonus(
            services,
            session,
            semantic.query_vector,
            chosen,
            [from_semantic, fallback, wiki_candidates, *translated_lists],
        )
        ranked = router.fuse(
            embedder,
            semantic.query_vector,
            from_semantic,
            fallback,
            k,
            wiki=wiki_candidates,
            translated=translated_lists,
            room_bonus=room_bonus,
            embed_private=host_is_allowed(
                embedder_endpoint(settings), settings.private_allowed_hosts
            ),
        )
    if rooms:  # a regra traria versões de fora das salas pedidas: com salas não se aplica
        outcome = historical.HistoricalOutcome(ranked)
    else:
        outcome = historical.apply(
            services, session, query, normalize_language(lang) if lang else None, ranked, chosen, k
        )
    ranked = _drop_rejected(session, outcome.candidates)
    latency_ms = round((time.perf_counter() - started) * 1000)
    logger.info(
        "search %r: sim=%s -> %s, %d results (level %s) in %d ms",
        query,
        f"{semantic.similarity:.3f}" if semantic.similarity is not None else "-",
        route.value,
        len(ranked),
        ranked[0].level if ranked else "-",
        latency_ms,
    )

    upsert_documents(session, [DocumentInfo(c.source, c.path, c.title) for c in ranked])
    wiki_urls = wiki_urls_for(
        session, services, [make_document_id(c.source.id, c.path) for c in ranked]
    )
    log = QueryLog(
        principal_id=principal_id,
        query=query,
        query_lang=normalize_language(lang) if lang else None,
        filters={
            "lang": lang,
            "sources": list(sources) if sources else None,
            "k": k,
            **({"wings": list(wings)} if wings else {}),
            **({"rooms": list(rooms)} if rooms else {}),
            **({"historical": list(outcome.reasons)} if outcome.reasons else {}),
            **({"translated_query": translated_query} if translated_query else {}),
        },
        level_served=ranked[0].level if ranked else None,
        top_score=ranked[0].score if ranked else None,
        latency_ms=latency_ms,
        session_id=session_id,
    )
    session.add(log)
    session.flush()
    document_rooms = rooms_index.rooms_of(
        session, [make_document_id(c.source.id, c.path) for c in ranked]
    )
    hits = [
        SearchHit(
            rank=rank,
            document_id=make_document_id(c.source.id, c.path),
            title=c.title,
            source_id=c.source.id,
            language=c.source.language,
            snippet=_shorten(c.snippet),
            level=c.level,
            score=round(c.score, 4),
            kiwix_url=_kiwix_url(services, c),
            wiki_url=wiki_urls.get(make_document_id(c.source.id, c.path)),
            chunk_id=c.chunk_id,
            wing=c.source.wing,
            room=document_rooms.get(make_document_id(c.source.id, c.path)),
            original_source=c.original,
            translated_query=c.translated,
            content_type=c.source.content_type,
        )
        for rank, c in enumerate(ranked, start=1)
    ]
    session.add_all(
        QueryResult(
            query_id=log.id,
            rank=hit.rank,
            document_id=hit.document_id,
            chunk_id=hit.chunk_id,
            level=hit.level,
            score=hit.score,
            original_source=hit.original_source,
            translated_query=hit.translated_query,
        )
        for hit in hits
    )
    _record_heat_and_index(services, session, ranked)
    hits = _with_books(services, session, query, hits, ranked)
    session.commit()
    return SearchResult(query_id=log.id, results=hits, warnings=warnings)


ROOM_OVERFETCH = 3  # com filtro por sala, o fallback pede k × isto antes de filtrar


def _wings_for_rooms(
    session: Session, wings: Sequence[str] | None, rooms: Sequence[int] | None
) -> list[str] | None:
    """As alas a pesquisar: as pedidas, ou (com `rooms`) as das salas pedidas. Recusa salas que
    não existem e salas de alas fora das pedidas."""
    if not rooms:
        return list(wings) if wings else None
    found = rooms_index.wings_of_rooms(session, rooms)
    missing = [room for room in rooms if room not in found]
    if missing:
        raise SearchError(
            f"Rooms do not exist: {missing}. Room ids come from `sources` (field `rooms`)."
        )
    room_wings = sorted(set(found.values()))
    if wings:
        outside = [w for w in room_wings if w not in set(wings)]
        if outside:
            raise SearchError(
                f"The requested rooms belong to wings {outside}, outside the requested wings."
            )
    return room_wings


def _only_rooms(
    session: Session, candidates: Sequence[Candidate], rooms: Sequence[int]
) -> list[Candidate]:
    """Dos resultados do fallback, os de documentos que estão numa das salas (os outros, ou os que
    ainda não têm sala, ficam de fora)."""
    found = rooms_index.room_ids_of_documents(
        session, [make_document_id(c.source.id, c.path) for c in candidates]
    )
    wanted = set(rooms)
    return [c for c in candidates if found.get(make_document_id(c.source.id, c.path)) in wanted]


def _room_bonus(
    services: Services,
    session: Session,
    query_vector: Sequence[float] | None,
    chosen: Sequence[Source],
    rankings: Sequence[Sequence[Candidate]],
) -> dict[tuple[str, str], float]:
    """O bónus de fusão dos resultados que estão numa das salas mais prováveis da pergunta (as
    `ROUTER_ROOMS_TOP` com o centróide mais perto): chave do candidato -> bónus."""
    settings = services.settings
    model_id = router.current_model_id(services)
    if query_vector is None or model_id is None or settings.router_room_bonus <= 0:
        return {}
    likely = rooms_index.likely_rooms(
        session,
        query_vector,
        model_id,
        sorted({s.wing for s in chosen}),
        top=settings.router_rooms_top,
    )
    if not likely:
        return {}
    candidates = {c.key: c for ranking in rankings for c in ranking}
    found = rooms_index.room_ids_of_documents(
        session, [make_document_id(*key) for key in candidates]
    )
    return {
        key: settings.router_room_bonus
        for key in candidates
        if found.get(make_document_id(*key)) in likely
    }


def _search_level2(services: Services, query: str, k: int) -> tuple[list[WikiHit], str | None]:
    """Pesquisa no Wiki.js. Corre numa thread: não toca na base de dados. Sem Wiki.js configurado
    devolve vazio e sem aviso; se falhar, devolve vazio e o aviso."""
    wiki = services.get_wiki()
    if wiki is None:
        return [], None
    try:
        return wiki.search(query, k), None
    except Exception as exc:  # o nível 2 nunca derruba a pesquisa (princípio 3)
        logger.warning("Level 2 unavailable: %s", exc)
        detail = str(exc) if isinstance(exc, WikiError) else type(exc).__name__
        return [], f"Wiki.js unavailable ({detail}); results without level 2."


# --- pergunta traduzida (STRUCTURE.md, secção 9) ---------------------------------------------

TRANSLATION_TARGET = {"pt": "en", "en": "pt"}  # PT⇄EN


def _translation_plan(
    services: Services, query: str, lang: str | None, chosen: Sequence[Source]
) -> tuple[str, str, list[Source]] | None:
    """(língua da pergunta, língua alvo, fontes alvo) se vale a pena traduzir a pergunta: há
    tradução configurada, a pergunta está em PT ou EN, o utilizador não restringiu a língua e a
    biblioteca tem fontes na outra. Sem rede."""
    if lang or not services.settings.translate_queries or not services.get_translation().enabled:
        return None
    source_lang = detect_language(query)
    target = TRANSLATION_TARGET.get(source_lang or "")
    if source_lang is None or target is None:
        return None
    targets = [s for s in chosen if s.language and normalize_language(s.language) == target]
    return (source_lang, target, targets) if targets else None


def _translate_query(
    services: Services, query: str, plan: tuple[str, str, list[Source]]
) -> tuple[str | None, str | None]:
    return services.get_translation().translate_query(query, plan[0], plan[1])


def _search_translated(
    services: Services,
    chosen: Sequence[Source],
    target: str,
    translation: "Future[tuple[str | None, str | None]]",
    k: int,
) -> tuple[str | None, list[Candidate], list[WikiHit], list[str]]:
    """Corre numa thread: espera a tradução da pergunta e, assim que chega, pesquisa-a nas fontes
    da outra língua (kiwix) e no Wiki.js, em paralelo. Devolve (pergunta traduzida, resultados do
    kiwix, resultados do Wiki.js, avisos). Não toca na base de dados nem em `warnings` do pedido."""
    translated, warning = translation.result()
    warnings = [warning] if warning else []
    if not translated:
        return None, [], [], warnings
    targets = [s for s in chosen if s.language and normalize_language(s.language) == target]
    with ThreadPoolExecutor(max_workers=1) as inner:
        wiki_future = inner.submit(_search_level2, services, translated, k)
        level1: list[Candidate] = []
        try:
            level1 = _rank(_search_level1(services, targets, translated, k, warnings), k)
        except SearchUnavailable as exc:
            warnings.append(f"Search for the translated question: {exc}")
        wiki_hits, wiki_warning = wiki_future.result()
    if wiki_warning:
        warnings.append(wiki_warning)
    return translated, level1, wiki_hits, warnings


def _wiki_candidates(
    session: Session, chosen: Sequence[Source], hits: Sequence[WikiHit]
) -> list[Candidate]:
    """Os resultados do Wiki.js que são páginas nossas (`wiki_page_id`), nas fontes pedidas."""
    if not hits:
        return []
    by_source = {s.id: s for s in chosen}
    rows = {
        row.wiki_page_id: row
        for row in session.scalars(
            select(DocumentRow)
            .outerjoin(Curation, Curation.document_id == DocumentRow.id)
            .where(
                DocumentRow.wiki_page_id.in_([h.page_id for h in hits]),
                Curation.archived_at.is_(None),  # as arquivadas e as retiradas já não valem
                or_(Curation.decision.is_(None), Curation.decision != "rejected"),
            )
        )
    }
    candidates: list[Candidate] = []
    for position, hit in enumerate(hits, start=1):
        row = rows.get(hit.page_id)
        source = by_source.get(row.source_id) if row is not None else None
        if row is None or source is None:
            continue
        candidates.append(
            Candidate(source, row.path, row.title, hit.description, level=2, score=1 / position)
        )
    return candidates


def _interleave(*rankings: list[Candidate]) -> list[Candidate]:
    """Listas já ordenadas, intercaladas por posição (sem repetir documentos)."""
    merged: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for position in range(max((len(ranking) for ranking in rankings), default=0)):
        for ranking in rankings:
            if position < len(ranking) and ranking[position].key not in seen:
                seen.add(ranking[position].key)
                merged.append(ranking[position])
    return merged


def _lesson_candidates(
    session: Session, chosen: Sequence[Source], query: str, k: int
) -> list[Candidate]:
    """As lições dos cursos que respondem à pergunta, por pesquisa de texto no índice (nível 2:
    as lições são o corpus curado dos cursos; entram na fusão com o Wiki.js e com o nível 3)."""
    by_source = {s.id: s for s in chosen if s.content_type == "lesson"}
    if not by_source:
        return []
    candidates: list[Candidate] = []
    for position, hit in enumerate(search_lessons(session, query, list(by_source), k), start=1):
        source = by_source.get(hit.source_id)
        if source is not None:
            candidates.append(
                Candidate(source, hit.path, hit.title, hit.snippet, level=2, score=1 / position)
            )
    return candidates


def _chapter_candidates(
    session: Session, chosen: Sequence[Source], query: str, k: int
) -> list[Candidate]:
    """Os capítulos dos livros que respondem à pergunta, por pesquisa de texto no índice (nível 2:
    os capítulos extraídos são o corpus curado dos livros)."""
    by_source = {s.id: s for s in chosen if s.content_type == "chapter"}
    if not by_source:
        return []
    candidates: list[Candidate] = []
    for position, hit in enumerate(search_chapters(session, query, list(by_source), k), start=1):
        source = by_source.get(hit.source_id)
        if source is not None:
            candidates.append(
                Candidate(
                    source,
                    hit.path,
                    hit.title,
                    hit.snippet,
                    level=2,
                    score=1 / position,
                    position=hit.position,
                )
            )
    return candidates


def _drop_rejected(session: Session, ranked: list[Candidate]) -> list[Candidate]:
    """Os capítulos rejeitados na revisão humana não aparecem nos resultados."""
    ids = {
        make_document_id(c.source.id, c.path): c
        for c in ranked
        if c.source.content_type == "chapter"
    }
    if not ids:
        return ranked
    rejected = set(
        session.scalars(
            select(Curation.document_id).where(
                Curation.document_id.in_(list(ids)), Curation.decision == "rejected"
            )
        )
    )
    return [c for c in ranked if make_document_id(c.source.id, c.path) not in rejected]


def _with_books(
    services: Services, session: Session, query: str, hits: list[SearchHit], ranked: list[Candidate]
) -> list[SearchHit]:
    """Nos capítulos: o livro, a posição da passagem e a citação."""
    chapters = [hit for hit in hits if hit.content_type == "chapter"]
    if not chapters:
        return hits
    infos = book_infos(
        session, [hit.document_id for hit in chapters], services.settings.kavita_public_url
    )
    terms = query_terms(query)
    by_key = {make_document_id(c.source.id, c.path): c for c in ranked}
    enriched: list[SearchHit] = []
    for hit in hits:
        info = infos.get(hit.document_id)
        if hit.content_type != "chapter" or info is None:
            enriched.append(hit)
            continue
        candidate = by_key[hit.document_id]
        position = (
            candidate.position
            if candidate.position is not None
            else locate(session, hit.document_id, candidate.snippet, terms)
        )
        enriched.append(
            hit.model_copy(
                update={
                    "book": info,
                    "position": position,
                    "citation": citation(hit.document_id, position),
                    "reader_url": info.reader_url,
                }
            )
        )
    return enriched


def _kiwix_url(services: Services, candidate: Candidate) -> str | None:
    book = candidate.source.meta.get("kiwix_book")
    if not book:
        return None
    return content_url(services.settings.kiwix_public_url, str(book), candidate.path)


def _record_heat_and_index(services: Services, session: Session, ranked: list[Candidate]) -> None:
    """Cada resultado mostrado aquece (0,3) e o que veio do fallback entra na fila de indexação
    com prioridade igual ao calor que já tem (secção 5.4)."""
    if not ranked:
        return
    half_life = services.settings.heat_half_life_days
    document_ids = [make_document_id(c.source.id, c.path) for c in ranked]
    heat.add_document_heat(session, document_ids, heat.WEIGHT_SHOWN, half_life)
    heat.add_chunk_heat(
        session,
        [c.chunk_id for c in ranked if c.chunk_id is not None],
        heat.WEIGHT_SHOWN,
        half_life,
    )
    fallback_ids = [make_document_id(c.source.id, c.path) for c in ranked if c.level != 3]
    gave_up = recently_failed(session, fallback_ids)  # falhou de vez há pouco: não insistir
    fallback_ids = [d for d in fallback_ids if d not in gave_up]
    model_id = router.current_model_id(services)
    if model_id is not None:
        done = services.store.indexed(session, fallback_ids, model_id)
        fallback_ids = [d for d in fallback_ids if d not in done]
    if not fallback_ids:
        return
    rows = session.execute(
        select(DocumentRow.id, heat.document_heat(half_life)).where(
            DocumentRow.id.in_(fallback_ids)
        )
    ).all()
    priorities = {row[0]: float(row[1]) for row in rows}
    for document_id in sorted(fallback_ids):
        enqueue_index(session, document_id, priorities.get(document_id, heat.WEIGHT_SHOWN))


def _search_level1(
    services: Services, chosen: Sequence[Source], query: str, k: int, warnings: list[str]
) -> list[Candidate]:
    """kiwix-serve só pesquisa vários livros se tiverem a mesma língua: um pedido por língua."""
    groups: dict[str, list[Source]] = defaultdict(list)
    for source in chosen:
        if not source.meta.get("kiwix_book"):
            continue  # as lições dos cursos não estão no kiwix: têm a sua pesquisa (nível 2)
        groups[str(source.meta.get("zim_language") or source.language)].append(source)

    def run(item: tuple[int, list[Source]]) -> list[Candidate] | SearchUnavailable:
        group, members = item
        try:
            return _search_group(services, group, members, query, k, warnings)
        except SearchUnavailable as exc:
            return exc

    items = list(enumerate(groups.values()))
    if not items:
        return []
    if len(items) == 1:
        outcomes = [run(items[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(items), 4)) as pool:
            outcomes = list(pool.map(run, items))
    failures = [o for o in outcomes if isinstance(o, SearchUnavailable)]
    if len(failures) == len(outcomes):
        raise failures[0]
    warnings.extend(str(failure) for failure in failures)  # uma língua falhou, as outras não
    return [
        c for outcome in outcomes if not isinstance(outcome, SearchUnavailable) for c in outcome
    ]


def _search_group(
    services: Services,
    group: int,
    members: list[Source],
    query: str,
    k: int,
    warnings: list[str],
) -> list[Candidate]:
    """Uma língua. Com o UUID do ZIM pesquisa-se por `books.id`, em lotes (o kiwix-serve recusa URLs
    muito compridos: ~570 livros não cabem num pedido) que correm em paralelo e se intercalam."""
    by_book = {str(s.meta["kiwix_book"]): s for s in members if s.meta.get("kiwix_book")}
    ids = [str(s.meta["uuid"]) for s in members if s.meta.get("kiwix_book") and s.meta.get("uuid")]
    # nos avisos, o livro pelo nome do ficheiro (o UUID não diz nada a quem lê)
    names = {str(s.meta["uuid"]): str(s.meta["kiwix_book"]) for s in members if s.meta.get("uuid")}
    if len(ids) < len(by_book):  # fontes antigas sem UUID: o caminho de sempre, por nome
        batches: list[tuple[bool, list[str]]] = [(False, list(by_book))]
    else:
        # Só os livros que o kiwix já tem (o catálogo dele); os ZIM novos que ainda não carregou
        # dão um aviso só, em vez de um pedido falhado cada um.
        known = services.kiwix.known_book_ids()
        if known is not None:
            absent = [names.get(i, i) for i in ids if i not in known]
            ids = [i for i in ids if i in known]
            if absent:
                sample = ", ".join(sorted(absent)[:3])
                more = f" and {len(absent) - 3} more" if len(absent) > 3 else ""
                warnings.append(
                    f"{len(absent)} book(s) are not in kiwix-serve yet ({sample}{more}): "
                    "they stay out of the text search until kiwix loads them."
                )
            if not ids:
                raise SearchUnavailable("kiwix-serve does not have any of this search's books yet.")
        size = max(1, services.settings.kiwix_search_batch)
        batches = [(True, ids[i : i + size]) for i in range(0, len(ids), size)]

    def run(batch: tuple[bool, list[str]]) -> list[KiwixHit] | SearchUnavailable | None:
        by_id, books = batch
        while books:
            try:
                if by_id:
                    return services.kiwix.search(query, k=k, book_ids=books)
                return services.kiwix.search(query, books=books, k=k)
            except KiwixBookMissing as exc:
                # ZIM acrescentado depois de o kiwix-serve arrancar: pesquisa-se o resto.
                warnings.append(str(KiwixBookMissing(names.get(exc.book, exc.book))))
                if exc.book not in books:  # o kiwix não disse qual dos pedidos: não há o que tirar
                    return SearchUnavailable(f"Search unavailable: {exc}.")
                books = [b for b in books if b != exc.book]
            except KiwixError as exc:
                return SearchUnavailable(f"Search unavailable: {exc}.")
        return None

    if len(batches) == 1:
        outcomes = [run(batches[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(batches), 4)) as pool:
            outcomes = list(pool.map(run, batches))
    results = [o for o in outcomes if isinstance(o, list)]
    if not results:
        failure = next((o for o in outcomes if isinstance(o, SearchUnavailable)), None)
        raise failure or SearchUnavailable(
            "kiwix-serve does not know any of the requested books (restart kiwix)."
        )
    warnings.extend(str(o) for o in outcomes if isinstance(o, SearchUnavailable))
    # Os lotes não têm scores comparáveis: intercala-se por posição (1.º de cada lote, 2.º…).
    merged = [hit for rank in zip_longest(*results) for hit in rank if hit is not None]
    kept = [
        h for h in merged if h.book in by_book and not by_book[h.book].archive_only_for(h.path)
    ][:k]
    # O kiwix não dá pontuação: fica 1/posição, que é 1/(1+índice) a contar de zero.
    return [
        Candidate(
            by_book[h.book],
            h.path,
            h.title,
            h.snippet,
            level=1,
            score=1 / position,
            group=group,
        )
        for position, h in enumerate(kept, start=1)
    ]


def _rank(candidates: list[Candidate], k: int) -> list[Candidate]:
    """Ordem final. Entre línguas não há scores comparáveis (o kiwix não os dá): intercala-se por
    posição, em round-robin, começando pela língua com mais resultados."""
    by_group: dict[int, list[Candidate]] = defaultdict(list)
    for candidate in candidates:  # já vêm por posição dentro de cada grupo
        by_group[candidate.group].append(candidate)
    order = sorted(by_group, key=lambda group: (-len(by_group[group]), group))
    longest = max((len(members) for members in by_group.values()), default=0)
    seen: set[tuple[str, str]] = set()
    ranked: list[Candidate] = []
    for position in range(longest):
        for group in order:
            if position >= len(by_group[group]):
                continue
            candidate = by_group[group][position]
            key = candidate.key
            if key not in seen:
                seen.add(key)
                ranked.append(candidate)
    return ranked[:k]


def _shorten(snippet: str) -> str:
    if len(snippet) <= SNIPPET_CHARS:
        return snippet
    return snippet[:SNIPPET_CHARS].rsplit(" ", 1)[0].rstrip(",;:.") + " …"


# --- feedback --------------------------------------------------------------------------------


def give_feedback(
    session: Session,
    principal_id: int,
    query_id: int,
    document_id: str,
    useful: bool,
    *,
    half_life_days: float = heat.HALF_LIFE_DAYS,
    warm: bool = True,
) -> int:
    """Marca o resultado como útil (+1) ou inútil (-1); devolve quantas linhas mudaram.
    Um feedback positivo aquece o documento (e o chunk, se o resultado era do nível 3), menos com
    `warm=False` (o de um cliente externo fica registado mas não mexe no ranking do dono)."""
    document_id = clean_text(document_id)
    owner = session.scalar(select(QueryLog.principal_id).where(QueryLog.id == query_id))
    if owner is None or owner != principal_id:
        raise FeedbackError(f"Search {query_id} not found (use the query_id returned by search).")
    values: dict[str, object] = {"feedback": 1 if useful else -1}
    if useful:
        values["used"] = True
    where = (QueryResult.query_id == query_id, QueryResult.document_id == document_id)
    # Bloqueia as linhas e vê o que lá estava: o calor do feedback positivo soma-se uma só vez por
    # par (pesquisa, documento); repetir a chamada (ou mudar de ideias e voltar) não o repete.
    before = session.execute(
        select(QueryResult.chunk_id, QueryResult.feedback).where(*where).with_for_update()
    ).all()
    if not before:
        raise FeedbackError(f"Document {document_id!r} is not in the results of search {query_id}.")
    session.execute(update(QueryResult).where(*where).values(**values))
    first_time = [chunk_id for chunk_id, feedback in before if feedback in (None, 0)]
    if warm and useful and first_time:
        heat.add_document_heat(session, [document_id], heat.WEIGHT_FEEDBACK, half_life_days)
        chunk_ids = [chunk_id for chunk_id in first_time if chunk_id is not None]
        heat.add_chunk_heat(session, chunk_ids, heat.WEIGHT_FEEDBACK, half_life_days)
    session.commit()
    return len(before)
