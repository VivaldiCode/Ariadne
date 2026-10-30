"""A árvore de domínios e a colocação dos documentos nela (docs/WIKI-IMPORT.md, secção 6).

`domains.yaml` traz cerca de 22 domínios de topo com os seus subdomínios (nomes, *slugs* e
descrições em `pt` e `en`) e os *priors* de fonte; `sync_tree` carrega-o para a tabela `domain`,
`ensure_vectors` vetoriza as descrições (para a comparação com os documentos) e `place` coloca um
documento: o prior da fonte, depois os vizinhos mais próximos entre as âncoras (os documentos já
colocados por `prior` ou por uma pessoa) em votação com as descrições, depois a estrutura da fonte,
e por fim `por-classificar`. O resultado fica em `domain_assignment` e uma colocação humana nunca é
sobreposta. O que fica sem certeza (`needs_review`) segue para o lote LLM `domain_classify`.
"""

import fnmatch
import functools
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pgvector.sqlalchemy import Vector
from sqlalchemy import delete, func, select, text, type_coerce
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne.db.models import (
    EMBEDDING_DIM,
    Chunk,
    ChunkVector,
    DocumentVector,
    DomainAssignment,
    DomainVector,
)
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Domain as DomainRow
from ariadne.embedder import Embedder
from ariadne.wiki import paths

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).with_name("domains.yaml")
FALLBACK_ID = "por-classificar"
LOCALES = ("pt", "en")
LOCALE_FALLBACK = "en"

# Calibração dos vetores (e5-small, cosseno; ver o relatório da Fase): com os 105 artigos da amostra
# o cosseno absoluto não distingue um bom de um mau resultado (lixo e artigos reais ficam em
# 0,80-0,86) e a margem para o 2.º é curta (mediana 0,012). O que separa é quantos desvios-padrão o
# melhor domínio fica acima da média das folhas (`z`): com z >= 3 acerta-se o domínio de topo em
# ~90% dos casos (55% dos documentos), com z < 3 em ~50%.
# Vai para revisão se z < REVIEW_Z, se a margem (melhor menos segundo) < REVIEW_MARGIN, ou se o
# melhor cosseno < REVIEW_TOP1 (uma rede de segurança: os três mais baixos da amostra, 0,75-0,78,
# estavam todos errados).
# Com z < FLOOR_Z, ou um cosseno abaixo de FLOOR_TOP1, nenhuma folha serve e o documento fica
# em `por-classificar`.
REVIEW_TOP1 = 0.78
REVIEW_MARGIN = 0.005
REVIEW_Z = 3.0
FLOOR_Z = 2.0
FLOOR_TOP1 = 0.6  # e abaixo disto nada tem a ver (o mínimo real da amostra é 0,75)
PRIOR_CONFIDENCE = 0.9  # uma fonte especializada fixa o domínio; não é uma medida vetorial
LLM_CONFIDENCE = 0.8  # a escolha do LLM entre os candidatos (não é uma medida vetorial)
STRUCTURE_CONFIDENCE = 0.7  # a maioria das páginas ligadas a partir da mesma página-índice
TOP_SIGNALS = 3  # quantos candidatos ficam registados em `signals`

# --- âncoras (kNN) e correção do atrator -------------------------------------------------------
# Os documentos colocados por `prior` (fontes especializadas) ou por uma pessoa são exemplos
# etiquetados. O cosseno do e5 é comprimido (0,80 ± 0,04 entre quaisquer textos), por isso um
# vizinho conta como «próximo» pelo z do seu cosseno contra todas as âncoras, não pelo valor
# absoluto: com z >= 1,5, k = 10 e pelo menos 5 vizinhos assim, o voto acertou 8 em 8 entre
# fontes diferentes na amostra e 2 em 3 nas fontes gerais (o que não dispara cai nas descrições).
KNN_K = 10
KNN_ZMIN = 1.5
KNN_MIN_NEIGHBORS = 5
KNN_MIN_COSINE = 0.75  # e o cosseno puro nunca baixo disto (lixo não vota)
KNN_ACCEPT_SHARE = 0.6  # fração do voto (ponderado) do domínio de topo vencedor para o aceitar
KNN_TRUST_SHARE = 0.85  # e a partir daqui aceita-se mesmo que as descrições discordem
ANCHOR_METHODS = ("prior", "human")
ANCHORS_PER_DOMAIN = 200  # no máximo estas âncoras por domínio (o voto não se afoga em saúde)
# O atrator: algumas folhas («sobrevivencia/abrigo-e-fogo») têm cosseno alto com tudo. Subtrai-se
# a cada folha a sua semelhança média a documentos ao acaso. Com a correção, o mesmo z e a mesma
# margem aceitam o dobro dos documentos com a mesma precisão (9 para 18 de 38 a 89%; amostra).
HUB_SAMPLE = 400  # documentos ao acaso para medir a semelhança média de cada folha
HUB_MIN_DOCS = 30  # com menos documentos que isto não há correção (a regra de sempre)
BIG_TABLE = 100_000  # vetores de artigo a partir dos quais a amostra se tira por sorteio de linhas
HUB_REVIEW_Z = 3.5
HUB_REVIEW_MARGIN = 0.01

METHODS = ("prior", "vector", "structure", "llm", "human", "fallback")
# Uma colocação destes métodos sobrevive a uma nova passagem automática (o `llm` foi decidido por
# um modelo maior e o `human` por uma pessoa; repetir os vetores não os pode desfazer).
PROTECTED_METHODS = ("human", "llm")
# A versão do algoritmo de colocação. Uma página colocada por uma versão anterior volta a ser
# colocada (e muda de caminho, com `pages.move`) na reimportação seguinte; sobe ao mudar as regras.
# 2: correção do atrator e kNN sobre âncoras.
PLACEMENT_VERSION = 2


class DomainError(ValueError):
    """O YAML está mal formado, ou o pedido refere um domínio ou documento que não existe."""


# --- o ficheiro ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainSpec:
    """Um nó do YAML, já achatado (os pais vêm antes dos filhos)."""

    id: str
    parent_id: str | None
    position: int
    names: Mapping[str, str]
    slugs: Mapping[str, str]
    descriptions: Mapping[str, str]


@dataclass(frozen=True)
class SourcePrior:
    pattern: str
    domain: str

    def matches(self, source_name: str) -> bool:
        """Sem `*`, o padrão é um prefixo do nome do ZIM; com `*`, um padrão de ficheiros."""
        pattern = self.pattern.lower()
        return fnmatch.fnmatchcase(source_name, pattern if "*" in pattern else pattern + "*")


@dataclass(frozen=True)
class DomainConfig:
    nodes: tuple[DomainSpec, ...]
    priors: tuple[SourcePrior, ...]

    def prior_for(self, source_name: str) -> SourcePrior | None:
        name = _bare_name(source_name)
        return next((p for p in self.priors if p.matches(name)), None)


def _bare_name(source_name: str) -> str:
    """'zim:Wikem_en_all' -> 'wikem_en_all'."""
    return source_name.split(":", 1)[-1].lower()


def parse_config(raw: Mapping[str, Any]) -> DomainConfig:
    """Valida e achata o YAML: ids únicos, `pt`/`en` em tudo, *slugs* únicos entre irmãos e
    *priors* que apontam para nós que existem."""
    nodes: list[DomainSpec] = []
    ids: set[str] = set()

    def walk(items: Sequence[Mapping[str, Any]], parent: str | None) -> None:
        slugs_seen: dict[str, set[str]] = {locale: set() for locale in LOCALES}
        for position, item in enumerate(items):
            node_id = str(item.get("id") or "")
            if not node_id or node_id in ids:
                raise DomainError(f"Domain without an id, or with a repeated id: {node_id!r}.")
            ids.add(node_id)
            fields: dict[str, dict[str, str]] = {}
            for key in ("names", "slugs", "descriptions"):
                value = item.get(key) or {}
                missing = [locale for locale in LOCALES if not str(value.get(locale) or "").strip()]
                if missing:
                    raise DomainError(f"Domain {node_id}: {key} missing for {', '.join(missing)}.")
                fields[key] = {locale: " ".join(str(value[locale]).split()) for locale in LOCALES}
            for locale in LOCALES:
                slug = fields["slugs"][locale]
                if slug in slugs_seen[locale]:
                    raise DomainError(
                        f"Domain {node_id}: the {locale} slug {slug!r} already exists."
                    )
                slugs_seen[locale].add(slug)
            nodes.append(
                DomainSpec(
                    id=node_id,
                    parent_id=parent,
                    position=position,
                    names=fields["names"],
                    slugs=fields["slugs"],
                    descriptions=fields["descriptions"],
                )
            )
            walk(item.get("children") or [], node_id)

    walk(raw.get("domains") or [], None)
    if FALLBACK_ID not in ids:
        raise DomainError(f"The special domain {FALLBACK_ID!r} is missing.")
    priors = tuple(
        SourcePrior(str(p["pattern"]).lower(), str(p["domain"]))
        for p in raw.get("source_priors") or []
    )
    unknown = sorted({p.domain for p in priors} - ids)
    if unknown:
        raise DomainError(f"The priors point to domains that do not exist: {unknown}.")
    return DomainConfig(tuple(nodes), priors)


@functools.cache
def _default_config() -> DomainConfig:
    return parse_config(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")))


def load_config(path: Path | None = None) -> DomainConfig:
    """O `domains.yaml` do pacote (em cache), ou o ficheiro dado."""
    if path is None:
        return _default_config()
    return parse_config(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- a tabela ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncResult:
    created: int
    updated: int
    deactivated: int


def sync_tree(session: Session, config: DomainConfig | None = None) -> SyncResult:
    """Carrega o YAML para `domain` (upsert). Os nós que saíram do YAML ficam inativos (não se
    apagam: as colocações antigas apontam para eles). Se a descrição de um nó mudou, o seu vetor
    deixa de valer e apaga-se (`ensure_vectors` volta a fazê-lo). Idempotente; não faz commit."""
    config = config or load_config()
    existing = {row.id: row for row in session.scalars(select(DomainRow))}
    created = updated = 0
    for spec in config.nodes:  # os pais antes dos filhos: a chave estrangeira existe sempre
        row = existing.get(spec.id)
        if row is None:
            session.add(
                DomainRow(
                    id=spec.id,
                    parent_id=spec.parent_id,
                    position=spec.position,
                    names=dict(spec.names),
                    slugs=dict(spec.slugs),
                    descriptions=dict(spec.descriptions),
                    active=True,
                )
            )
            session.flush()
            created += 1
            continue
        changed = (
            row.parent_id != spec.parent_id
            or row.position != spec.position
            or row.names != dict(spec.names)
            or row.slugs != dict(spec.slugs)
            or row.descriptions != dict(spec.descriptions)
            or not row.active
        )
        if row.descriptions != dict(spec.descriptions) or row.names != dict(spec.names):
            session.execute(delete(DomainVector).where(DomainVector.domain_id == spec.id))
        if changed:
            row.parent_id = spec.parent_id
            row.position = spec.position
            row.names = dict(spec.names)
            row.slugs = dict(spec.slugs)
            row.descriptions = dict(spec.descriptions)
            row.active = True
            updated += 1
    wanted = {spec.id for spec in config.nodes}
    deactivated = 0
    for row in existing.values():
        if row.id not in wanted and row.active:
            row.active = False
            deactivated += 1
    session.flush()
    return SyncResult(created, updated, deactivated)


def node_texts(names: Mapping[str, str], descriptions: Mapping[str, str]) -> list[str]:
    """Os textos que se vetorizam de um domínio: «Nome. Descrição.» em cada língua."""
    return [f"{names[locale]}. {descriptions[locale]}" for locale in LOCALES]


def ensure_vectors(session: Session, embedder: Embedder, model_id: str) -> int:
    """Vetoriza as descrições dos domínios ativos que ainda não têm vetor para `model_id` (o
    embedder aplica o prefixo `passage: `). Um domínio tem um só vetor: a média normalizada dos
    vetores das duas línguas. Devolve quantos criou. Não faz commit."""
    if embedder.model_id != model_id:
        raise DomainError(f"The embedder is {embedder.model_id}, not {model_id}.")
    have = set(
        session.scalars(select(DomainVector.domain_id).where(DomainVector.model_id == model_id))
    )
    todo = [
        row
        for row in session.scalars(
            select(DomainRow).where(DomainRow.active, DomainRow.id != FALLBACK_ID)
        )
        if row.id not in have
    ]
    if not todo:
        return 0
    texts = [text for row in todo for text in node_texts(row.names, row.descriptions)]
    vectors = embedder.embed_passages(texts)
    if len(vectors) != len(texts):
        raise DomainError("The embedder returned a different number of vectors than requested.")
    step = len(LOCALES)
    rows = [
        {
            "domain_id": row.id,
            "model_id": model_id,
            "embedding": _unit(np.mean(np.asarray(vectors[i * step : (i + 1) * step]), axis=0)),
        }
        for i, row in enumerate(todo)
    ]
    stmt = insert(DomainVector).values(rows)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["domain_id", "model_id"], set_={"embedding": stmt.excluded.embedding}
        )
    )
    return len(rows)


def slugs_for(session: Session, domain_id: str, locale: str) -> list[str]:
    """Os *slugs* da raiz até ao nó (`['transportes', 'automoveis']`) no locale dado, com `en` como
    reserva quando o locale não tem *slug* próprio."""
    chain: list[str] = []
    current: str | None = domain_id
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise DomainError(f"The domain tree has a cycle at {current!r}.")
        seen.add(current)
        row = session.get(DomainRow, current)
        if row is None:
            raise DomainError(f"Domain {current!r} does not exist.")
        chain.append(
            row.slugs.get(locale) or row.slugs.get(LOCALE_FALLBACK) or row.id.rsplit("/", 1)[-1]
        )
        current = row.parent_id
    return chain[::-1]


# --- colocar -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    domain_id: str
    confidence: float
    method: str
    signals: dict[str, Any] = field(default_factory=dict)
    needs_review: bool = False


def _unit(vector: Any) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return array / norm if norm else array


@dataclass(frozen=True)
class _Scored:
    ranked: list[tuple[str, float]]  # (domínio, pontuação) do melhor para o pior (só os candidatos)
    mean: float  # média e desvio das pontuações contra TODAS as folhas, para o z do melhor
    std: float
    # O cosseno do melhor (sem a correção do atrator) e se `ranked` já a leva. Sem ela, a
    # pontuação é o próprio cosseno.
    best_cosine: float | None = None
    corrected: bool = False

    @property
    def top1(self) -> float:
        return self.ranked[0][1]

    @property
    def raw_top1(self) -> float:
        return self.top1 if self.best_cosine is None else self.best_cosine

    @property
    def margin(self) -> float:
        return self.top1 - self.ranked[1][1] if len(self.ranked) > 1 else self.top1

    @property
    def z(self) -> float:
        return (self.top1 - self.mean) / self.std if self.std > 0 else 0.0

    @property
    def weak(self) -> bool:
        z_min, margin_min = (
            (HUB_REVIEW_Z, HUB_REVIEW_MARGIN) if self.corrected else (REVIEW_Z, REVIEW_MARGIN)
        )
        return self.z < z_min or self.margin < margin_min or self.raw_top1 < REVIEW_TOP1

    @property
    def hopeless(self) -> bool:
        return self.z < FLOOR_Z or self.raw_top1 < FLOOR_TOP1

    def signals(self, source: str, model_id: str) -> dict[str, Any]:
        found: dict[str, Any] = {
            "source": source,
            "model_id": model_id,
            "top": [{"domain": d, "score": round(s, 4)} for d, s in self.ranked[:TOP_SIGNALS]],
            "top1": round(self.top1, 4),
            "margin": round(self.margin, 4),
            "z": round(self.z, 2),
        }
        if self.corrected:
            found["cosine"] = round(self.raw_top1, 4)
            found["hub_corrected"] = True
        return found


def document_vector(
    session: Session, document_id: str, model_id: str
) -> tuple[np.ndarray, str] | None:
    """O vetor do documento: o centróide dos vetores dos chunks (o texto todo; na amostra acerta
    mais do que o vetor de artigo: 74% contra 60%) ou, se ainda não há chunks, o vetor de artigo
    (título + 1.º parágrafo). Devolve também de onde veio."""
    embeddings = session.scalars(
        select(ChunkVector.embedding)
        .join(Chunk, Chunk.id == ChunkVector.chunk_id)
        .where(Chunk.document_id == document_id, Chunk.ord >= 0, ChunkVector.model_id == model_id)
    ).all()
    if embeddings:
        matrix = np.stack([np.asarray(e, dtype=np.float32) for e in embeddings])
        return _unit(matrix.mean(axis=0)), "chunks"
    row = session.get(DocumentVector, (document_id, model_id))
    if row is not None:
        return _unit(row.embedding), "document_vector"
    return None


@dataclass(frozen=True)
class TreeSnapshot:
    """A árvore ativa e os vetores das folhas de um modelo, carregados de uma vez: quem coloca
    muitos documentos seguidos (o importador) carrega-a uma vez e reutiliza-a em `place`, em vez de
    voltar a ler os domínios e os seus vetores a cada documento. Leva também as âncoras do kNN e
    a semelhança média de cada folha a documentos ao acaso (`hub`, o atrator)."""

    model_id: str
    parents: Mapping[str, str | None]  # os domínios ativos: id -> pai
    leaves: frozenset[str]  # os ativos sem filhos ativos (menos `por-classificar`)
    vectors: Mapping[str, np.ndarray]  # folha -> vetor unitário (só as que têm vetor)
    index_paths: Mapping[str, frozenset[str]]  # locale -> caminhos das páginas de domínio
    hub: Mapping[str, float] = field(default_factory=dict)  # folha -> cosseno médio (vazio: sem)
    anchor_ids: tuple[str, ...] = ()  # os documentos-âncora, na ordem das linhas da matriz
    anchor_domains: tuple[str, ...] = ()  # e o domínio de cada um
    anchor_matrix: np.ndarray | None = None  # N x d, vetores unitários (vetor de artigo)
    anchor_hub: np.ndarray | None = None  # N: semelhança média de cada âncora a documentos ao acaso

    def index_paths_for(self, locale: str) -> frozenset[str]:
        return self.index_paths.get(locale) or self.index_paths.get(LOCALE_FALLBACK, frozenset())

    def under(self, domain_id: str, ancestor: str) -> bool:
        current: str | None = domain_id
        while current is not None:
            if current == ancestor:
                return True
            current = self.parents.get(current)
        return False

    def root_of(self, domain_id: str) -> str | None:
        """O domínio de topo de um nó ativo; `None` se o nó não está na árvore."""
        if domain_id not in self.parents:
            return None
        current = domain_id
        while self.parents.get(current) is not None:
            current = str(self.parents[current])
        return current


def load_snapshot(session: Session, model_id: str) -> TreeSnapshot:
    """Lê a árvore ativa, os vetores das folhas, as âncoras e o atrator para `model_id`."""
    rows = {r.id: r for r in session.scalars(select(DomainRow).where(DomainRow.active))}
    parents = {i: r.parent_id for i, r in rows.items()}
    with_children = {p for p in parents.values() if p}
    leaves = frozenset(i for i in rows if i not in with_children and i != FALLBACK_ID)
    vectors = {
        domain_id: _unit(embedding)
        for domain_id, embedding in session.execute(
            select(DomainVector.domain_id, DomainVector.embedding).where(
                DomainVector.model_id == model_id, DomainVector.domain_id.in_(sorted(leaves))
            )
        )
    }

    def chain(domain_id: str, locale: str) -> list[str]:
        out: list[str] = []
        current: str | None = domain_id
        while current is not None and current in rows and len(out) <= len(rows):
            row = rows[current]
            out.append(
                row.slugs.get(locale) or row.slugs.get(LOCALE_FALLBACK) or row.id.rsplit("/", 1)[-1]
            )
            current = row.parent_id
        return out[::-1]

    index_paths = {
        locale: frozenset(paths.domain_index_path(chain(i, locale)) for i in rows)
        for locale in LOCALES
    }
    ids, anchor_domains, matrix = _load_anchors(session, model_id, set(rows))
    sample = _sample_vectors(session, model_id)
    hub: dict[str, float] = {}
    anchor_hub: np.ndarray | None = None
    if sample is not None:
        hub = {leaf: float((sample @ vector).mean()) for leaf, vector in vectors.items()}
        if matrix is not None:
            anchor_hub = (matrix @ sample.T).mean(axis=1)
    return TreeSnapshot(
        model_id,
        parents,
        leaves,
        vectors,
        index_paths,
        hub=hub,
        anchor_ids=ids,
        anchor_domains=anchor_domains,
        anchor_matrix=matrix,
        anchor_hub=anchor_hub,
    )


def _load_anchors(
    session: Session, model_id: str, active: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...], np.ndarray | None]:
    """As âncoras: os documentos colocados por `prior` ou por uma pessoa que têm vetor de artigo,
    no máximo `ANCHORS_PER_DOMAIN` por domínio (uma escolha estável, por hash do id)."""
    ranked = (
        select(
            DomainAssignment.document_id.label("document_id"),
            DomainAssignment.domain_id.label("domain_id"),
            DocumentVector.embedding.label("embedding"),
            func.row_number()
            .over(
                partition_by=DomainAssignment.domain_id,
                order_by=func.md5(DomainAssignment.document_id),
            )
            .label("n"),
        )
        .join(DocumentVector, DocumentVector.document_id == DomainAssignment.document_id)
        .where(
            DocumentVector.model_id == model_id,
            DomainAssignment.method.in_(ANCHOR_METHODS),
            DomainAssignment.domain_id != FALLBACK_ID,
        )
        .subquery()
    )
    found = [
        (document_id, domain_id, embedding)
        for document_id, domain_id, embedding in session.execute(
            select(ranked.c.document_id, ranked.c.domain_id, ranked.c.embedding)
            .where(ranked.c.n <= ANCHORS_PER_DOMAIN)
            .order_by(ranked.c.document_id)
        )
        if domain_id in active
    ]
    if not found:
        return (), (), None
    matrix = np.stack([_unit(embedding) for _, _, embedding in found])
    return tuple(i for i, _, _ in found), tuple(d for _, d, _ in found), matrix


def _sample_vectors(session: Session, model_id: str) -> np.ndarray | None:
    """Os vetores (unitários) de até `HUB_SAMPLE` documentos ao acaso, como `document_vector` os
    daria (o centróide dos chunks; o vetor de artigo se ainda não há chunks): servem para medir o
    atrator. `None` se há menos de `HUB_MIN_DOCS` (então não se corrige nada)."""
    total = session.scalar(
        text("SELECT reltuples::bigint FROM pg_class WHERE relname = 'document_vector'")
    )
    picked = select(DocumentVector.document_id, DocumentVector.embedding).where(
        DocumentVector.model_id == model_id
    )
    if total is not None and total > BIG_TABLE:  # tabela grande: sorteia-se por páginas
        percent = min(100.0, 100.0 * 4 * HUB_SAMPLE / total)
        picked = picked.where(text(f"random() < {percent / 100:.6f}"))
    sampled = picked.order_by(func.random()).limit(HUB_SAMPLE).subquery()
    centroid = (
        select(type_coerce(func.avg(ChunkVector.embedding), Vector(EMBEDDING_DIM)))
        .join(Chunk, Chunk.id == ChunkVector.chunk_id)
        .where(
            Chunk.document_id == sampled.c.document_id,
            Chunk.ord >= 0,
            ChunkVector.model_id == model_id,
        )
        .scalar_subquery()
    )
    embeddings = [
        e for (e,) in session.execute(select(func.coalesce(centroid, sampled.c.embedding)))
    ]
    if len(embeddings) < HUB_MIN_DOCS:
        return None
    return np.stack([_unit(e) for e in embeddings])


def score_leaves(
    session: Session | None,
    query: np.ndarray,
    model_id: str,
    *,
    within: str | None = None,
    snapshot: TreeSnapshot | None = None,
) -> _Scored | None:
    """A pontuação do vetor `query` contra cada folha: o cosseno menos a semelhança média da folha
    a documentos ao acaso (o atrator, `TreeSnapshot.hub`; sem ela, o cosseno). A ordem (e a
    margem) é só entre as folhas de baixo de `within`, se dado; o `z` mede-se sempre contra todas.
    None se não há vetores de domínios para o modelo. Com `snapshot` não lê a base de dados."""
    if snapshot is None:
        assert session is not None
        snapshot = load_snapshot(session, model_id)
    tree_now = snapshot
    vectors = tree_now.vectors
    if not vectors:
        return None
    ids = sorted(vectors)
    cosines = np.stack([vectors[i] for i in ids]) @ _unit(query)
    corrected = bool(tree_now.hub)
    hub = np.array([tree_now.hub.get(i, 0.0) for i in ids]) if corrected else np.zeros(len(ids))
    scores = cosines - hub
    mean, std = float(scores.mean()), float(scores.std())
    pairs = list(zip(ids, (float(x) for x in scores), strict=True))
    raw = dict(zip(ids, (float(x) for x in cosines), strict=True))
    if within is not None:
        pairs = [(i, x) for i, x in pairs if tree_now.under(i, within)]
    if not pairs:
        return None
    ranked = sorted(pairs, key=lambda p: (-p[1], p[0]))
    return _Scored(ranked, mean, std, raw[ranked[0][0]], corrected)


# --- o voto dos vizinhos (kNN) ---------------------------------------------------------------


@dataclass(frozen=True)
class KnnVote:
    """O voto das âncoras mais próximas, por domínio de topo."""

    top: str  # o domínio de topo vencedor
    share: float  # a fração do voto (ponderado) que tem
    margin: float  # o avanço sobre o segundo
    neighbors: int  # vizinhos próximos que votaram
    ranked: tuple[tuple[str, float], ...]  # (domínio de topo, fração) do melhor para o pior

    def signals(self) -> dict[str, Any]:
        return {
            "top": self.top,
            "share": round(self.share, 3),
            "margin": round(self.margin, 3),
            "neighbors": self.neighbors,
            "votes": [{"domain": d, "share": round(f, 3)} for d, f in self.ranked[:TOP_SIGNALS]],
        }


def knn_vote(
    query: np.ndarray, snapshot: TreeSnapshot, *, exclude: str | None = None
) -> KnnVote | None:
    """Vota nos `KNN_K` vizinhos mais próximos do `query` entre as âncoras. Um vizinho é próximo se
    o z da sua semelhança (o cosseno menos o atrator da âncora, contra todas as âncoras) é pelo
    menos `KNN_ZMIN` e o cosseno pelo menos `KNN_MIN_COSINE`; cada um pesa
    `z - KNN_ZMIN + 0,5`. `None` se há menos de `KNN_MIN_NEIGHBORS` vizinhos próximos (poucas
    âncoras, ou nenhuma perto): então decidem as descrições. `exclude` é o próprio documento."""
    matrix = snapshot.anchor_matrix
    if matrix is None or len(matrix) < KNN_MIN_NEIGHBORS + 1:
        return None
    cosines = matrix @ _unit(query)
    sims = cosines
    if snapshot.anchor_hub is not None:  # um vizinho que se parece com tudo não prova nada
        sims = cosines - snapshot.anchor_hub
    keep = np.ones(len(sims), dtype=bool)
    if exclude is not None and exclude in snapshot.anchor_ids:
        keep[snapshot.anchor_ids.index(exclude)] = False
    if int(keep.sum()) < KNN_MIN_NEIGHBORS + 1:
        return None
    mean, std = float(sims[keep].mean()), float(sims[keep].std())
    if std <= 0:
        return None
    votes: dict[str, float] = {}
    close = 0
    for index in np.argsort(-np.where(keep, sims, -np.inf))[:KNN_K]:
        z = (float(sims[index]) - mean) / std
        root = snapshot.root_of(snapshot.anchor_domains[index])
        if z < KNN_ZMIN or cosines[index] < KNN_MIN_COSINE or root is None or not keep[index]:
            continue
        votes[root] = votes.get(root, 0.0) + (z - KNN_ZMIN + 0.5)
        close += 1
    if close < KNN_MIN_NEIGHBORS:
        return None
    total = sum(votes.values())
    ranked = tuple(sorted(((d, v / total) for d, v in votes.items()), key=lambda p: (-p[1], p[0])))
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    return KnnVote(ranked[0][0], ranked[0][1], ranked[0][1] - second, close, ranked)


def place(
    session: Session,
    *,
    document_id: str,
    source_name: str,
    embedder_model_id: str,
    structure_hint: str | None = None,
    snapshot: TreeSnapshot | None = None,
) -> Placement:
    """Coloca o documento na árvore e grava em `domain_assignment` (sem commit). Pela ordem:

    1. o *prior* da fonte (`source_priors`). Se aponta para um domínio com filhos, os vetores
       escolhem o filho quando têm provas firmes; senão fica o domínio do prior;
    2. o voto dos vizinhos mais próximos entre as âncoras (`knn_vote`), quando há pelo menos
       `KNN_MIN_NEIGHBORS` próximos e se entendem (`KNN_ACCEPT_SHARE`); sem revisão se as
       descrições concordam ou a votação é esmagadora;
    3. a semelhança vetorial do documento com as folhas (menos o atrator): `needs_review` se o
       melhor domínio não se destaca (`z`), a margem para o segundo é curta ou o cosseno é baixo
       (`REVIEW_TOP1`); se nem destaca de todo (`z` < `FLOOR_Z`) nada serve;
    4. `structure_hint` (o domínio da maioria das páginas ligadas a partir da mesma página-índice),
       quando os vetores faltam ou são fracos;
    5. `por-classificar`.

    `needs_review` quer dizer sem certeza: quem coloca (o importador) enfileira o job
    `domain_classify` para o lote LLM.

    Uma colocação `human` (ou `llm`) nunca é sobreposta: devolve-se a que lá está.

    `snapshot` (de `load_snapshot`) poupa ler a árvore e os vetores dos domínios a cada chamada;
    deve ser do mesmo `embedder_model_id`. Sem ele, lê-os agora."""
    current = session.get(DomainAssignment, document_id)
    if current is not None and current.method in PROTECTED_METHODS:
        return _placement_of(current)

    if snapshot is None or snapshot.model_id != embedder_model_id:
        snapshot = load_snapshot(session, embedder_model_id)
    placement = _decide(
        session, document_id, source_name, embedder_model_id, structure_hint, snapshot
    )
    _store(session, document_id, placement)
    return placement


def _placement_of(row: DomainAssignment) -> Placement:
    return Placement(row.domain_id, row.confidence, row.method, dict(row.signals), row.needs_review)


def _store(session: Session, document_id: str, placement: Placement) -> None:
    stmt = insert(DomainAssignment).values(
        document_id=document_id,
        domain_id=placement.domain_id,
        confidence=placement.confidence,
        method=placement.method,
        signals=placement.signals,
        needs_review=placement.needs_review,
        updated_at=datetime.now(UTC),
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["document_id"],
            set_={
                "domain_id": stmt.excluded.domain_id,
                "confidence": stmt.excluded.confidence,
                "method": stmt.excluded.method,
                "signals": stmt.excluded.signals,
                "needs_review": stmt.excluded.needs_review,
                "updated_at": stmt.excluded.updated_at,
            },
            # A guarda final contra uma corrida: nunca se sobrepõe o que uma pessoa ou o LLM fez.
            where=DomainAssignment.method.not_in(PROTECTED_METHODS),
        )
    )


def _decide(
    session: Session,
    document_id: str,
    source_name: str,
    model_id: str,
    structure_hint: str | None,
    snapshot: TreeSnapshot,
) -> Placement:
    query = document_vector(session, document_id, model_id)
    return decide_vectors(query, document_id, source_name, model_id, structure_hint, snapshot)


def decide_vectors(
    query: tuple[np.ndarray, str] | None,
    document_id: str,
    source_name: str,
    model_id: str,
    structure_hint: str | None,
    snapshot: TreeSnapshot,
) -> Placement:
    """A decisão de `place` sem base de dados: `query` é o vetor do documento e de onde veio
    (`document_vector`), ou `None` se ainda não tem; tudo o resto está em `snapshot`."""
    active = set(snapshot.parents)
    signals: dict[str, Any] = {}

    scored = score_leaves(None, query[0], model_id, snapshot=snapshot) if query else None
    if query is None:
        signals["vector"] = "the document has no vector yet"
    elif scored is None:
        signals["vector"] = "the domains have no vectors yet (ensure_vectors)"

    prior = load_config().prior_for(source_name)
    if prior is not None and prior.domain in active:
        signals["prior"] = {"pattern": prior.pattern, "domain": prior.domain}
        domain_id = prior.domain
        if query is not None and prior.domain not in snapshot.leaves:
            inside = score_leaves(None, query[0], model_id, within=prior.domain, snapshot=snapshot)
            if inside is not None and not inside.weak:
                signals["refined"] = inside.signals(query[1], model_id)
                domain_id = inside.ranked[0][0]
        return Placement(domain_id, PRIOR_CONFIDENCE, "prior", signals, needs_review=False)
    if prior is not None:
        signals["prior_ignored"] = f"domain {prior.domain!r} does not exist or is inactive"

    voted, contested = _vote_placement(query, scored, snapshot, document_id, model_id, signals)
    if voted is not None:
        return voted
    if contested:  # as âncoras e as descrições discordam: quem decidir, decide em revisão
        placement = _by_descriptions(query, scored, active, model_id, structure_hint, signals)
        return replace(placement, needs_review=True)
    return _by_descriptions(query, scored, active, model_id, structure_hint, signals)


def _by_descriptions(
    query: tuple[np.ndarray, str] | None,
    scored: _Scored | None,
    active: set[str],
    model_id: str,
    structure_hint: str | None,
    signals: dict[str, Any],
) -> Placement:
    """As descrições (menos o atrator), a estrutura da fonte e `por-classificar`."""
    if scored is not None and query is not None and not scored.hopeless:
        signals.update(vector=scored.signals(query[1], model_id))
        best = scored.ranked[0][0]
        if not scored.weak:
            return Placement(best, scored.margin, "vector", signals, needs_review=False)
        if structure_hint in active and structure_hint != FALLBACK_ID:
            top = [d for d, _ in scored.ranked[:TOP_SIGNALS]]
            signals["structure"] = {"hint": structure_hint, "in_vector_top": structure_hint in top}
            return Placement(
                str(structure_hint),
                STRUCTURE_CONFIDENCE,
                "structure",
                signals,
                needs_review=structure_hint not in top,
            )
        return Placement(best, scored.margin, "vector", signals, needs_review=True)
    if scored is not None and query is not None:
        signals["vector"] = scored.signals(query[1], model_id)
        signals["floor"] = (
            f"the best domain is only {scored.z:.1f} deviations above the mean (< {FLOOR_Z})"
        )

    if structure_hint in active and structure_hint != FALLBACK_ID:
        signals["structure"] = {"hint": structure_hint, "in_vector_top": None}
        return Placement(
            str(structure_hint), STRUCTURE_CONFIDENCE, "structure", signals, needs_review=True
        )
    if structure_hint is not None:
        signals["structure_ignored"] = f"domain {structure_hint!r} does not exist or is inactive"
    return Placement(FALLBACK_ID, 0.0, "fallback", signals, needs_review=True)


def _vote_placement(
    query: tuple[np.ndarray, str] | None,
    scored: _Scored | None,
    snapshot: TreeSnapshot,
    document_id: str,
    model_id: str,
    signals: dict[str, Any],
) -> tuple[Placement | None, bool]:
    """O voto das âncoras: (colocação, contestada). Com uma votação esmagadora (`KNN_TRUST_SHARE`),
    ou boa (`KNN_ACCEPT_SHARE`) e com as descrições a concordar no domínio de topo, coloca-se sem
    revisão no vencedor (a folha, dentro dele, é a melhor pelas descrições). Boa mas contrariada
    pelas descrições: sem colocação, decidem as descrições e a página vai para revisão
    (`contestada`). Sem votação que chegue, `(None, False)`."""
    if query is None:
        return None, False
    vote = knn_vote(query[0], snapshot, exclude=document_id)
    if vote is None:
        return None, False
    signals["knn"] = vote.signals()
    if vote.share < KNN_ACCEPT_SHARE:
        return None, False
    agree = scored is not None and snapshot.root_of(scored.ranked[0][0]) == vote.top
    signals["knn"]["descriptions_agree"] = agree
    if not (agree or vote.share >= KNN_TRUST_SHARE):
        return None, True
    if scored is not None:
        signals["vector"] = scored.signals(query[1], model_id)
    inside = score_leaves(None, query[0], model_id, within=vote.top, snapshot=snapshot)
    domain_id = inside.ranked[0][0] if inside is not None else vote.top
    return Placement(domain_id, vote.share, "vector", signals, needs_review=False), False


CANDIDATES = 8  # domínios que o lote LLM (`domain_classify`) recebe para escolher


def candidates(
    query: tuple[np.ndarray, str] | None,
    snapshot: TreeSnapshot,
    model_id: str,
    document_id: str,
    limit: int = CANDIDATES,
) -> list[str]:
    """Os `limit` domínios (folhas) em que o documento pode estar, para o LLM escolher: primeiro
    a melhor folha de cada domínio de topo votado pelas âncoras (até metade), depois as melhores
    pelas descrições (menos o atrator). Sem vetor do documento, os domínios de topo."""
    found: list[str] = []
    if query is not None:
        vote = knn_vote(query[0], snapshot, exclude=document_id)
        for top, _ in (vote.ranked if vote is not None else ())[: limit // 2]:
            inside = score_leaves(None, query[0], model_id, within=top, snapshot=snapshot)
            if inside is not None and inside.ranked[0][0] not in found:
                found.append(inside.ranked[0][0])
        scored = score_leaves(None, query[0], model_id, snapshot=snapshot)
        for domain_id, _ in scored.ranked if scored is not None else ():
            if len(found) >= limit:
                break
            if domain_id not in found:
                found.append(domain_id)
    if not found:
        found = [d for d, parent in snapshot.parents.items() if parent is None and d != FALLBACK_ID]
    return found[:limit] if query is not None else found


# --- a colocação humana e a vista da árvore -------------------------------------------------------


def assign_human(
    session: Session, *, document_id: str, domain_id: str, reviewer: str, note: str | None = None
) -> Placement:
    """Uma pessoa põe o documento num domínio: `method='human'`, confiança 1, sem revisão. Fica
    para sempre (as passagens automáticas não lhe tocam). Sem commit."""
    if session.get(DocumentRow, document_id) is None:
        raise DomainError(f"Document {document_id!r} does not exist.")
    domain = session.get(DomainRow, domain_id)
    if domain is None or not domain.active:
        raise DomainError(f"Domain {domain_id!r} does not exist or is inactive.")
    previous = session.get(DomainAssignment, document_id)
    signals: dict[str, Any] = {"reviewer": reviewer}
    if note:
        signals["note"] = note
    if previous is not None:
        signals["previous"] = {
            "domain": previous.domain_id,
            "method": previous.method,
            "confidence": previous.confidence,
        }
    placement = Placement(domain_id, 1.0, "human", signals, needs_review=False)
    stmt = insert(DomainAssignment).values(
        document_id=document_id,
        domain_id=domain_id,
        confidence=1.0,
        method="human",
        signals=signals,
        needs_review=False,
        updated_at=datetime.now(UTC),
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["document_id"],
            set_={
                "domain_id": stmt.excluded.domain_id,
                "confidence": stmt.excluded.confidence,
                "method": stmt.excluded.method,
                "signals": stmt.excluded.signals,
                "needs_review": stmt.excluded.needs_review,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    )
    return placement


def assign_llm(
    session: Session,
    *,
    document_id: str,
    domain_id: str,
    model: str | None,
    run_id: int | None,
    candidates: Sequence[str],
    needs_review: bool,
) -> Placement:
    """O lote LLM (`domain_classify`) decidiu o domínio: `method='llm'`, que as passagens
    automáticas já não mudam (uma pessoa ainda pode). `needs_review` se a escolha ficou fora dos
    candidatos. Sem commit."""
    if session.get(DocumentRow, document_id) is None:
        raise DomainError(f"Document {document_id!r} does not exist.")
    domain = session.get(DomainRow, domain_id)
    if domain is None or not domain.active or domain_id == FALLBACK_ID:
        raise DomainError(f"Domain {domain_id!r} does not exist or is inactive.")
    previous = session.get(DomainAssignment, document_id)
    if previous is not None and previous.method == "human":
        return _placement_of(previous)
    signals: dict[str, Any] = {
        "llm": {"model": model, "run_id": run_id, "candidates": list(candidates)}
    }
    if previous is not None:
        signals["previous"] = {
            "domain": previous.domain_id,
            "method": previous.method,
            "confidence": previous.confidence,
        }
    placement = Placement(domain_id, LLM_CONFIDENCE, "llm", signals, needs_review=needs_review)
    stmt = insert(DomainAssignment).values(
        document_id=document_id,
        domain_id=domain_id,
        confidence=LLM_CONFIDENCE,
        method="llm",
        signals=signals,
        needs_review=needs_review,
        updated_at=datetime.now(UTC),
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["document_id"],
            set_={
                "domain_id": stmt.excluded.domain_id,
                "confidence": stmt.excluded.confidence,
                "method": stmt.excluded.method,
                "signals": stmt.excluded.signals,
                "needs_review": stmt.excluded.needs_review,
                "updated_at": stmt.excluded.updated_at,
            },
            where=DomainAssignment.method != "human",  # uma pessoa manda mais do que o modelo
        )
    )
    return placement


def tree(session: Session) -> list[dict[str, Any]]:
    """A árvore para a API: cada nó com os nomes, *slugs*, descrições, os documentos colocados
    diretamente nele (`documents`), os de toda a subárvore (`documents_total`) e os que esperam
    revisão (`needs_review`), mais os `children` por ordem. Os nós inativos vêm marcados."""
    rows = list(session.scalars(select(DomainRow).order_by(DomainRow.position, DomainRow.id)))
    counts = {
        domain_id: (total, review)
        for domain_id, total, review in session.execute(
            select(
                DomainAssignment.domain_id,
                func.count(),
                func.count().filter(DomainAssignment.needs_review),
            ).group_by(DomainAssignment.domain_id)
        )
    }
    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        own, review = counts.get(row.id, (0, 0))
        nodes[row.id] = {
            "id": row.id,
            "parent_id": row.parent_id,
            "position": row.position,
            "names": row.names,
            "slugs": row.slugs,
            "descriptions": row.descriptions,
            "active": row.active,
            "documents": own,
            "documents_total": own,
            "needs_review": review,
            "children": [],
        }
    roots: list[dict[str, Any]] = []
    for row in rows:
        node = nodes[row.id]
        parent = nodes.get(row.parent_id) if row.parent_id else None
        (parent["children"] if parent else roots).append(node)

    def total(node: dict[str, Any]) -> int:
        node["documents_total"] = node["documents"] + sum(total(c) for c in node["children"])
        return int(node["documents_total"])

    for root in roots:
        total(root)
    return roots
