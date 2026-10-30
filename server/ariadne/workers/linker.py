"""Worker `linker`: liga documentos de fontes diferentes (ARCHITECTURE.md, secção 7.2).

Periodicamente vê os documentos que ainda não viu (`document.linked_at IS NULL`: novos, ou com o
título, os títulos alternativos ou o vetor de artigo alterados) e cria arestas em `link`:

- `title_match`: o título (ou um título alternativo, de um redirect do ZIM) normalizado é igual ao
  de um documento de outra fonte ativa. Só vale com igualdade dos títulos principais e a mesma
  língua; por alias, ou entre línguas diferentes, fica `proposed` (score = cosseno dos vetores de
  artigo, se os há), a não ser que o cosseno ≥ `LINK_TAU` o confirme. Ignoram-se os títulos curtos,
  só numéricos ou genéricos, e os que identificam mais de `LINK_TITLE_MAX_PER_SOURCE` documentos
  de uma fonte (não identificam nada);
- `vector`: o vetor de artigo tem semelhança cosseno ≥ `LINK_TAU` com o de um documento de outra
  fonte; ≥ `LINK_TAU_ACCEPT` fica aceite, entre os dois limiares fica `proposed` à espera de
  curadoria.

Cada par de documentos vê-se a partir dos dois lados (o último a chegar encontra o que já lá
estava), por isso as arestas são **não dirigidas** e guardam-se uma só vez, com
`src_document < dst_document`: ver `ariadne.graph.canonical`. Uma aresta que já existe não se
toca (uma rejeitada pela curadoria não ressuscita), exceto uma proposta do próprio linker que um
vetor passa a confirmar.

Um documento só fica visto (`linked_at`) quando o seu lote acabou. Enquanto isso tem uma concessão
(`link_lease`, que caduca) e uma tentativa contada: se o lote falha, tentam-se os documentos um a
um, e um que falhe `LINK_MAX_ATTEMPTS` vezes seguidas deixa de ser tentado sem travar os outros.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne import router
from ariadne.db.models import Link
from ariadne.graph import canonical
from ariadne.services import Services
from ariadne.textutil import title_key
from ariadne.wiki.refresh import enqueue_refresh

logger = logging.getLogger(__name__)

CREATED_BY = "linker"
TITLE_MATCH, VECTOR = "title_match", "vector"
CHUNK = 100  # documentos por transação
MIN_KEY_CHARS = 4
# Títulos que existem em toda a parte e não identificam nada (já normalizados).
GENERIC_TITLES = frozenset(
    {
        "main page", "home", "index", "contents", "table of contents", "about", "introduction",
        "glossary", "bibliography", "references", "notes", "sandbox", "disambiguation", "list",
        "página principal", "índice", "indice", "introdução", "glossário", "bibliografia",
        "referências", "notas", "sumário", "início", "pagina principal", "portada",
    }
)  # fmt: skip


def usable_key(key: str) -> bool:
    """Uma chave de título serve para ligar: não é curta, nem só números, nem genérica."""
    return (
        len(key) >= MIN_KEY_CHARS
        and not key.replace(" ", "").isdigit()
        and key not in GENERIC_TITLES
    )


@dataclass
class LinkReport:
    """O que uma execução fez (para o registo e para os testes)."""

    documents: int = 0
    title_links: int = 0  # `title_match` novos (aceites ou propostos)
    title_proposed: int = 0  # dos quais ficaram à espera de curadoria
    accepted: int = 0  # ligações `vector` aceites logo
    proposed: int = 0  # ligações `vector` à espera de curadoria
    failed: int = 0  # documentos que falharam (e foram isolados)
    backlog: bool = False  # parou no limite do lote com mais por ver: o runner volta já
    # Documentos cuja lista de arestas aceites mudou: os que têm página no Wiki.js refazem-na.
    touched: set[str] = field(default_factory=set)

    def add(self, other: "LinkReport") -> None:
        self.title_links += other.title_links
        self.title_proposed += other.title_proposed
        self.accepted += other.accepted
        self.proposed += other.proposed
        self.touched |= other.touched


def run_linker(services: Services, stop: threading.Event | None = None) -> LinkReport:
    """Uma execução: vê até `LINK_BATCH` documentos por ver e para entre lotes se `stop`."""
    settings = services.settings
    stop = stop or threading.Event()
    model_id = router.current_model_id(services)
    if model_id is None:  # sem saber o modelo não se sabe que vetores valem: espera-se
        logger.info("Linker: embedder unavailable, the run is postponed")
        return LinkReport()
    report = LinkReport()
    claimed_any = True
    failed_ids: list[str] = []  # nesta execução não se voltam a tentar
    while report.documents < settings.link_batch and not stop.is_set():
        size = min(CHUNK, settings.link_batch - report.documents)
        with services.sessions() as session:
            claimed, lease = _claim(services, session, size, failed_ids)
            session.commit()  # a concessão fica visível e os locks soltam-se: o resto é leitura
        if not claimed:
            claimed_any = False
            break
        failed_ids += _process(services, claimed, lease, model_id, report)
        report.documents += len(claimed)
    report.backlog = claimed_any and report.documents >= settings.link_batch and not stop.is_set()
    if report.documents:
        logger.info(
            "Linker: %d documents seen (%d failed), %d title links (%d proposed), "
            "%d vector links accepted, %d proposed",
            report.documents,
            report.failed,
            report.title_links,
            report.title_proposed,
            report.accepted,
            report.proposed,
        )
    return report


def _claim(
    services: Services, session: Session, size: int, skip: list[str] | None = None
) -> tuple[list[str], datetime | None]:
    """Reclama documentos por ver (num só UPDATE com SKIP LOCKED: dois workers nunca veem os
    mesmos): põe-lhes uma concessão e conta uma tentativa. Os que já esgotaram as tentativas, ou
    cuja concessão ainda não caducou, ficam de fora."""
    settings = services.settings
    rows = session.execute(
        text(
            "WITH picked AS (SELECT id FROM document WHERE linked_at IS NULL"
            "   AND link_attempts < :max_attempts AND NOT (id = ANY(CAST(:skip AS text[])))"
            "   AND (link_lease IS NULL OR link_lease < now() - :lease * interval '1 minute')"
            "   ORDER BY id LIMIT :size FOR UPDATE SKIP LOCKED)"
            " UPDATE document SET link_lease = now(), link_attempts = link_attempts + 1"
            " FROM picked WHERE document.id = picked.id"
            " RETURNING document.id, document.link_lease"
        ),
        {
            "size": size,
            "skip": skip or [],
            "max_attempts": settings.link_max_attempts,
            "lease": settings.link_lease_minutes,
        },
    ).all()
    return sorted(row[0] for row in rows), (rows[0][1] if rows else None)


def _process(
    services: Services,
    document_ids: list[str],
    lease: datetime | None,
    model_id: str,
    report: LinkReport,
) -> list[str]:
    """Liga o lote numa transação; se falha, tenta os documentos um a um para isolar o que falha.
    Devolve os que falharam."""
    attempt = LinkReport()
    try:
        with services.sessions() as session:
            _link(services, session, document_ids, model_id, attempt)
            _finish(session, document_ids, lease)
            session.commit()
        report.add(attempt)
        return []
    except Exception:
        logger.warning(
            "Linker: a batch of %d documents failed; trying one by one", len(document_ids)
        )
    failed: list[str] = []
    for document_id in document_ids:
        single = LinkReport()
        try:
            with services.sessions() as session:
                _link(services, session, [document_id], model_id, single)
                _finish(session, [document_id], lease)
                session.commit()
            report.add(single)
        except Exception:
            failed.append(document_id)
            report.failed += 1
            logger.exception("Linker: document %s failed (the attempt is counted)", document_id)
    return failed


def _finish(session: Session, document_ids: list[str], lease: datetime | None) -> None:
    """Dá por vistos os documentos cuja concessão ainda é esta: se algo os alterou entretanto
    (alias, vetor, título) a concessão foi apagada e continuam por ver."""
    session.execute(
        text(
            "UPDATE document SET linked_at = now(), link_lease = NULL, link_attempts = 0"
            " WHERE id = ANY(:ids) AND link_lease = :lease"
        ),
        {"ids": document_ids, "lease": lease},
    )


def _link(
    services: Services, session: Session, document_ids: list[str], model_id: str, report: LinkReport
) -> None:
    _set_title_keys(session, document_ids)
    _title_match(services, session, document_ids, model_id, report)
    settings = services.settings
    with_vector = services.store.has_article_vectors(session, document_ids, model_id)
    edges: dict[tuple[str, str], tuple[float, str]] = {}
    for document_id in document_ids:
        if document_id not in with_vector:
            continue
        for other, similarity in services.store.similar_across_sources(
            session, document_id, model_id, settings.link_tau, settings.link_max_per_doc
        ):
            status = "accepted" if similarity >= settings.link_tau_accept else "proposed"
            edges[canonical(document_id, other)] = (similarity, status)
    if edges:
        stmt = insert(Link).values(
            [
                {
                    "src_document": src,
                    "dst_document": dst,
                    "kind": VECTOR,
                    "score": score,
                    "model_id": model_id,
                    "status": status,
                    "created_by": CREATED_BY,
                }
                for (src, dst), (score, status) in sorted(edges.items())
            ]
        )
        created = session.execute(
            stmt.on_conflict_do_nothing(
                index_elements=["src_document", "dst_document", "kind"]
            ).returning(Link.status, Link.src_document, Link.dst_document)
        ).all()
        report.accepted += sum(1 for status, _, _ in created if status == "accepted")
        report.proposed += sum(1 for status, _, _ in created if status == "proposed")
        report.touched |= {d for status, a, b in created if status == "accepted" for d in (a, b)}
    enqueue_refresh(session, report.touched)  # as páginas dos que ganharam/perderam uma ligação


def _set_title_keys(session: Session, document_ids: list[str]) -> None:
    """Calcula (em Python, a única normalização) as chaves de título dos documentos."""
    rows = session.execute(
        text("SELECT id, title, meta FROM document WHERE id = ANY(:ids)"), {"ids": document_ids}
    )
    updates: list[tuple[str, list[str]]] = []
    for document_id, title, meta in rows:
        keys = {title_key(t) for t in [title, *meta.get("aliases", [])]}
        updates.append((document_id, sorted(k for k in keys if k)))
    session.execute(
        text("UPDATE document SET title_keys = :keys WHERE id = :id"),
        [{"id": doc_id, "keys": keys} for doc_id, keys in updates],
    )


@dataclass
class _Pair:
    key: str
    strong: bool = False  # títulos principais iguais e a mesma língua
    keys: set[str] = field(default_factory=set)


def _title_match(
    services: Services, session: Session, document_ids: list[str], model_id: str, report: LinkReport
) -> None:
    """`title_match` dos documentos do lote com os de outras fontes ativas."""
    settings = services.settings
    # Um documento cujo título mudou já não liga aos de antes: apagam-se (só as do linker, que
    # ninguém reviu) as arestas que deixaram de partilhar chave; as que continuam recriam-se.
    deleted = session.execute(
        text(
            "DELETE FROM link l USING document a, document b"
            " WHERE l.kind = 'title_match' AND l.created_by = :by AND l.reviewed_by IS NULL"
            "   AND a.id = ANY(:ids)"
            "   AND ((l.src_document = a.id AND l.dst_document = b.id)"
            "     OR (l.dst_document = a.id AND l.src_document = b.id))"
            "   AND NOT (a.title_keys && b.title_keys)"
            " RETURNING l.src_document, l.dst_document"
        ),
        {"ids": document_ids, "by": CREATED_BY},
    )
    report.touched |= {d for row in deleted for d in row}
    keys = {
        key
        for key in session.scalars(
            text("SELECT DISTINCT unnest(title_keys) FROM document WHERE id = ANY(:ids)"),
            {"ids": document_ids},
        )
        if usable_key(key)
    }
    if not keys:
        return
    wanted = sorted(keys)
    crowded = set(
        session.scalars(
            text(
                "SELECT k.key FROM document d CROSS JOIN LATERAL unnest(d.title_keys) AS k(key)"
                " WHERE d.title_keys && CAST(:keys AS text[])"
                "   AND k.key = ANY(CAST(:keys AS text[]))"
                " GROUP BY k.key, d.source_id HAVING count(*) > :max"
            ),
            {"keys": wanted, "max": settings.link_title_max_per_source},
        )
    )
    wanted = [key for key in wanted if key not in crowded]
    if not wanted:
        return
    rows = session.execute(
        text(
            "SELECT a.id, b.id, k.key, a.title, b.title, a.language, b.language"
            " FROM document a CROSS JOIN LATERAL unnest(a.title_keys) AS k(key)"
            " JOIN document b ON b.title_keys @> ARRAY[k.key] AND b.source_id <> a.source_id"
            " JOIN source sa ON sa.id = a.source_id AND sa.status = 'active'"
            " JOIN source sb ON sb.id = b.source_id AND sb.status = 'active'"
            " WHERE a.id = ANY(:ids) AND k.key = ANY(CAST(:keys AS text[]))"
        ),
        {"ids": document_ids, "keys": wanted},
    )
    pairs: dict[tuple[str, str], _Pair] = {}
    for a, b, key, a_title, b_title, a_lang, b_lang in rows:
        pair = pairs.setdefault(canonical(a, b), _Pair(key))
        pair.key = min(pair.key, key)
        same_language = a_lang is None or b_lang is None or a_lang == b_lang
        if same_language and title_key(a_title) == key == title_key(b_title):
            pair.strong = True
    # Um tecto de arestas novas por chave e execução: uma chave nunca deve inundar o grafo.
    per_key: dict[str, int] = {}
    chosen: dict[tuple[str, str], _Pair] = {}
    for pair_ids in sorted(pairs, key=lambda p: (pairs[p].key, p)):
        pair = pairs[pair_ids]
        if per_key.get(pair.key, 0) < settings.link_max_per_key:
            per_key[pair.key] = per_key.get(pair.key, 0) + 1
            chosen[pair_ids] = pair
    weak = [ids for ids, pair in chosen.items() if not pair.strong]
    cosines = services.store.cosine_similarities(session, model_id, weak)
    values = []
    for (src, dst), pair in sorted(chosen.items()):
        cosine = cosines.get((src, dst))
        score: float | None
        if pair.strong:
            score, status = 1.0, "accepted"
        else:  # por alias ou entre línguas: só um vetor parecido o confirma
            confirmed = cosine is not None and cosine >= settings.link_tau
            score, status = cosine, "accepted" if confirmed else "proposed"
        values.append(
            {
                "src_document": src,
                "dst_document": dst,
                "kind": TITLE_MATCH,
                "score": score,
                "model_id": model_id if cosine is not None and not pair.strong else None,
                "status": status,
                "created_by": CREATED_BY,
            }
        )
    if not values:
        return
    stmt = insert(Link).values(values)
    created = session.execute(
        stmt.on_conflict_do_update(  # uma proposta do linker que um vetor passa a confirmar
            index_elements=["src_document", "dst_document", "kind"],
            set_={
                "status": stmt.excluded.status,
                "score": stmt.excluded.score,
                "model_id": stmt.excluded.model_id,
            },
            where=(
                (Link.status == "proposed")
                & (Link.created_by == CREATED_BY)
                & Link.reviewed_by.is_(None)
                & (stmt.excluded.status == "accepted")
            ),
        ).returning(Link.status, Link.src_document, Link.dst_document)
    ).all()
    report.title_links += len(created)
    report.title_proposed += sum(1 for status, _, _ in created if status == "proposed")
    report.touched |= {d for status, a, b in created if status == "accepted" for d in (a, b)}
