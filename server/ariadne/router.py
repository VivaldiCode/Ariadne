"""Router da pesquisa (ARCHITECTURE.md, secção 5): nível 3 primeiro, fallback conforme a confiança.

    sim ≥ τ_alto            -> só o nível 3 (a cache semântica chega)
    τ_baixo ≤ sim < τ_alto  -> nível 3 + fallback, fundidos por RRF (zona cinzenta)
    sim < τ_baixo           -> só o fallback (nível 2, Wiki.js, e nível 1, kiwix, fundidos)

`sim` é a semelhança cosseno do melhor chunk. O fallback (níveis 2 e 1) vem já pesquisado de quem
chama (`search`); aqui só se decide e se funde. Tudo o que depende do modelo
degrada para o nível 1 se o embedder falhar (princípio 3).
"""

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from sqlalchemy.orm import Session

from ariadne.chunking import strip_title
from ariadne.embedder import Embedder, EmbedderError
from ariadne.services import Services
from ariadne.sources.base import Source

logger = logging.getLogger(__name__)

RRF_K = 60  # constante da Reciprocal Rank Fusion
CHUNKS_PER_RESULT = 10  # chunks pedidos ao índice por resultado final (vários são do mesmo artigo)
MIN_CHUNKS = 100


class Route(StrEnum):
    SEMANTIC = "semantic"  # só o nível 3
    FUSED = "fused"  # nível 3 + fallback
    FALLBACK = "fallback"  # só o fallback (inclui o embedder em baixo)


@dataclass(frozen=True)
class Candidate:
    """Um resultado antes de ter posição final, venha de que nível vier."""

    source: Source
    path: str
    title: str
    snippet: str
    level: int  # 1 = kiwix, 2 = wiki, 3 = semântico
    score: float
    group: int = 0  # grupo de línguas de onde veio o do nível 1 (desempate estável)
    chunk_id: int | None = None  # só no nível 3
    original: bool = False  # a versão na língua original do conceito (regra histórica)
    translated: bool = False  # só a pergunta traduzida a encontrou
    position: int | None = None  # nos capítulos: onde está a passagem (se já se sabe)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source.id, self.path)


@dataclass(frozen=True)
class SemanticOutcome:
    candidates: list[Candidate]  # um por documento (o melhor chunk), do mais ao menos parecido
    similarity: float | None  # a do melhor chunk; None sem vetores ou sem embedder
    query_vector: list[float] | None  # None se o embedder falhou


def decide(similarity: float | None, tau_high: float, tau_low: float) -> Route:
    if similarity is None or similarity < tau_low:
        return Route.FALLBACK
    return Route.SEMANTIC if similarity >= tau_high else Route.FUSED


def current_model_id(services: Services) -> str | None:
    """O `model_id` dos vetores de hoje, ou `None` se o embedder nem está configurado."""
    try:
        return services.get_embedder().model_id
    except EmbedderError:
        return None


def search_semantic(
    services: Services,
    session: Session,
    query: str,
    sources: Sequence[Source],
    k: int,
    warnings: list[str],
    room_ids: Sequence[int] | None = None,
) -> SemanticOutcome:
    """Embedding da pergunta e top-k em `chunk_vector` (nas fontes dadas, e nas salas dadas se
    houver); nunca levanta por causa do modelo: sem embedder devolve um resultado vazio e avisa."""
    try:
        embedder = services.get_embedder()
        vector = embedder.embed_queries([query])[0]
        model_id = embedder.model_id
    except EmbedderError as exc:
        logger.warning("Level 3 unavailable, serving from level 1: %s", exc)
        warnings.append(f"Semantic search unavailable ({exc}); level 1 results.")
        return SemanticOutcome([], None, None)

    by_id = {s.id: s for s in sources}
    hits = services.store.search(
        session,
        vector,
        model_id=model_id,
        k=max(MIN_CHUNKS, k * CHUNKS_PER_RESULT),
        source_ids=list(by_id),
        room_ids=room_ids,
    )
    session.commit()  # fecha a transação da consulta: o kiwix e o rerank não a seguram aberta
    candidates: dict[tuple[str, str], Candidate] = {}
    for hit in hits:  # já por semelhança: o primeiro chunk de cada artigo é o melhor
        source = by_id.get(hit.source_id)
        if source is None or (hit.source_id, hit.path) in candidates:
            continue
        if source.archive_only_for(hit.path):  # uma secção que só serve de arquivo
            continue
        candidates[(hit.source_id, hit.path)] = Candidate(
            source=source,
            path=hit.path,
            title=hit.title,
            snippet=strip_title(hit.text, hit.title),
            level=3,
            score=hit.similarity,
            chunk_id=hit.chunk_id,
        )
    return SemanticOutcome(list(candidates.values()), hits[0].similarity if hits else None, vector)


def fuse(
    embedder: Embedder | None,
    query_vector: Sequence[float] | None,
    semantic: Sequence[Candidate],
    fallback: Sequence[Candidate],
    k: int,
    wiki: Sequence[Candidate] = (),
    translated: Sequence[Sequence[Candidate]] = (),
    room_bonus: Mapping[tuple[str, str], float] | None = None,
    embed_private: bool = True,
) -> list[Candidate]:
    """RRF das listas: nível 3, nível 2 (Wiki.js) e nível 1 (kiwix). Antes, as do fallback são
    re-pontuadas com o embedding de título + excerto (secção 5.3): nem o kiwix nem o Wiki.js dão
    pontuações comparáveis, e assim a ordem deles passa a pesar o sentido. Um documento que vem
    de mais de uma lista fica com a versão do nível mais alto (3, depois 2, depois 1).

    `translated` são as listas da pergunta traduzida (secção 9), que entram na mesma fusão; o
    resultado que **só** elas encontraram fica marcado `translated`.

    `room_bonus` (chave do candidato -> bónus) soma-se ao RRF dos resultados das salas mais
    prováveis da pergunta (secção 5, regra 5): desempata a favor da sala certa, sem mandar na
    ordem."""
    scores: dict[tuple[str, str], float] = defaultdict(float)
    chosen: dict[tuple[str, str], Candidate] = {}
    original = (
        semantic,
        rerank(embedder, query_vector, wiki, embed_private),
        rerank(embedder, query_vector, fallback, embed_private),
    )
    extra = tuple(
        rerank(embedder, query_vector, candidates, embed_private) for candidates in translated
    )
    for ranking in (*original, *extra):  # o do nível 3 vem primeiro: mantém o chunk
        for position, candidate in enumerate(ranking, start=1):
            scores[candidate.key] += 1 / (RRF_K + position)
            chosen.setdefault(candidate.key, candidate)
    for key, bonus in (room_bonus or {}).items():
        if key in scores:
            scores[key] += bonus
    found_originally = {c.key for ranking in original for c in ranking}
    order = sorted(scores, key=lambda key: -scores[key])  # empates: ordem de chegada
    return [
        replace(chosen[key], score=scores[key], translated=key not in found_originally)
        for key in order[:k]
    ]


def rerank(
    embedder: Embedder | None,
    query_vector: Sequence[float] | None,
    candidates: Sequence[Candidate],
    embed_private: bool = True,
) -> list[Candidate]:
    """Ordena por semelhança entre a pergunta e «título. excerto»; sem modelo, deixa a ordem. Com
    `embed_private` falso (o embedder é remoto, fora de `PRIVATE_ALLOWED_HOSTS`), uma lista com
    resultados privados fica como está: o excerto privado não segue para o embedder."""
    if embedder is None or query_vector is None or len(candidates) < 2:
        return list(candidates)
    if not embed_private and any(c.source.license_class.value == "private" for c in candidates):
        return list(candidates)
    try:
        vectors = embedder.embed_passages([f"{c.title}. {c.snippet}" for c in candidates])
    except EmbedderError as exc:
        logger.warning("No re-scoring of the fallback: %s", exc)
        return list(candidates)
    sims = [sum(a * b for a, b in zip(query_vector, v, strict=True)) for v in vectors]
    order = sorted(range(len(candidates)), key=lambda i: -sims[i])
    return [candidates[i] for i in order]
