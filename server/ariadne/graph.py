"""O grafo de ligações entre documentos (ARCHITECTURE.md, secção 7): `neighbors` e a curadoria.

As arestas de `link` (exceto `zim_internal` «A cita B» e `requires` «A requer B», que têm sentido)
são **não dirigidas** e
guardam-se uma só vez com `src_document < dst_document` (ver `canonical`); todas as consultas
olham para os dois lados. Um vizinho semântico não é uma aresta: é o k-vizinho mais próximo nos
vetores de artigo, calculado na hora.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import REAL, case, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from ariadne import router
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Link
from ariadne.db.models import Source as SourceRow
from ariadne.documents import DocumentNotFound, LibraryError
from ariadne.services import Services
from ariadne.textutil import clean_text
from ariadne.wiki.refresh import enqueue_refresh

ZIM_INTERNAL = "zim_internal"
REQUIRES = "requires"  # «A requer B» (pré-requisito entre lições e entre cursos)
SEMANTIC = "semantic"  # não é uma aresta guardada
LINK_KINDS = (ZIM_INTERNAL, "title_match", "wikidata", "vector", "langlink", "manual", REQUIRES)
NEIGHBOR_KINDS = (*LINK_KINDS, SEMANTIC)
MANUAL = "manual"
DEFAULT_K = 20
MAX_K = 100
MAX_LIST = 200

Status = Literal["proposed", "accepted", "rejected"]


class LinkNotFound(LibraryError):
    pass


class LinkError(LibraryError):
    pass


def canonical(a: str, b: str) -> tuple[str, str]:
    """A ordem em que uma aresta não dirigida se guarda (a mesma do `COLLATE "C"` do Postgres)."""
    return (a, b) if a < b else (b, a)


# --- neighbors ------------------------------------------------------------------------------------


class NeighborInfo(BaseModel):
    document_id: str
    title: str
    source_id: str
    language: str | None
    kind: str  # title_match | vector | manual | zim_internal | semantic | …
    score: float | None  # 1 = igual; a semelhança cosseno nas ligações vetoriais e semânticas
    status: str  # accepted | proposed | computed (vizinho semântico, não é uma aresta)
    # Só `zim_internal` («A cita B») e `requires` («A requer B») têm sentido: `out` = este
    # documento cita/requer o vizinho, `in` = o vizinho cita/requer este.
    direction: Literal["both", "out", "in"] = "both"
    link_id: int | None = None  # a aresta, para a curadoria; `None` nos semânticos
    model_id: str | None = None


class NeighborsResult(BaseModel):
    document_id: str
    title: str
    neighbors: list[NeighborInfo]
    flags: list[str] = []  # no MCP: `title_injection` se algum título parece instruções

    def call_summary(self) -> dict[str, Any]:
        """O que fica em `tool_call.result`: ids e contagens."""
        return {
            "document_id": self.document_id,
            "count": len(self.neighbors),
            "kinds": sorted({n.kind for n in self.neighbors}),
            "document_ids": [n.document_id for n in self.neighbors],
        }


def check_kinds(kinds: list[str] | None) -> set[str] | None:
    if not kinds:
        return None
    wanted = {clean_text(kind).strip().lower() for kind in kinds}
    if unknown := sorted(wanted - set(NEIGHBOR_KINDS)):
        raise LibraryError(
            f"Unknown link types: {', '.join(unknown)}. Available: {', '.join(NEIGHBOR_KINDS)}."
        )
    return wanted


def neighbors(
    services: Services,
    session: Session,
    document_id: str,
    *,
    kinds: list[str] | None = None,
    k: int = DEFAULT_K,
) -> NeighborsResult:
    """Os documentos ligados ao dado: arestas (outras fontes, mesma fonte, manuais; as
    rejeitadas e as de fontes que sumiram não aparecem, as propostas vêm marcadas) e vizinhos
    semânticos. A ordem e o limite fazem-se na base de dados, por blocos: ligações a outras fontes
    (aceites e depois propostas), semânticos e, no que sobrar de `k`, os `zim_internal`."""
    document_id = clean_text(document_id)
    wanted = check_kinds(kinds)
    k = max(1, min(k, MAX_K))
    document = session.get(DocumentRow, document_id)
    if document is None:
        raise DocumentNotFound(
            f"Unknown document: {document_id!r}. Use the document_id from `search` or `read`."
        )
    found = _edges(session, document_id, wanted, internal=False, limit=k)
    if len(found) < k and (wanted is None or SEMANTIC in wanted):
        found.extend(_semantic(services, session, document_id, k - len(found), wanted))
    if len(found) < k and (wanted is None or ZIM_INTERNAL in wanted):
        found.extend(_edges(session, document_id, wanted, internal=True, limit=k - len(found)))
    return NeighborsResult(document_id=document.id, title=document.title, neighbors=found)


def _edges(
    session: Session, document_id: str, wanted: set[str] | None, *, internal: bool, limit: int
) -> list[NeighborInfo]:
    """Arestas do documento (nos dois sentidos) para fontes ativas, sem as rejeitadas: ou só as
    `zim_internal`, ou só as outras (aceites, depois propostas; da mais à menos pontuada)."""
    other = aliased(DocumentRow)
    query = (
        select(Link, other)
        .join(
            other,
            ((Link.src_document == document_id) & (other.id == Link.dst_document))
            | ((Link.dst_document == document_id) & (other.id == Link.src_document)),
        )
        .join(SourceRow, SourceRow.id == other.source_id)
        .where(
            Link.status != "rejected",
            SourceRow.status == "active",
            (Link.kind == ZIM_INTERNAL) if internal else (Link.kind != ZIM_INTERNAL),
        )
        .order_by(
            case((Link.status == "proposed", 1), else_=0),
            Link.score.desc().nulls_last(),
            other.id,
            Link.kind,
        )
        .limit(limit)
    )
    if wanted is not None:
        query = query.where(Link.kind.in_(wanted))
    found: list[NeighborInfo] = []
    for link, linked in session.execute(query):
        outgoing = link.src_document == document_id
        found.append(
            NeighborInfo(
                document_id=linked.id,
                title=linked.title,
                source_id=linked.source_id,
                language=linked.language,
                kind=link.kind,
                score=link.score,
                status=link.status,
                direction=("out" if outgoing else "in")
                if link.kind in (ZIM_INTERNAL, REQUIRES)
                else "both",
                link_id=link.id,
                model_id=link.model_id,
            )
        )
    return found


def _semantic(
    services: Services, session: Session, document_id: str, room: int, wanted: set[str] | None
) -> list[NeighborInfo]:
    """Até `room` documentos mais próximos nos vetores de artigo que ainda não vêm por uma aresta
    (dos tipos pedidos). O kNN já deixa de fora as fontes que sumiram e as ligações rejeitadas."""
    model_id = router.current_model_id(services)
    if model_id is None:
        return []
    nearest = services.store.nearest_documents(session, document_id, model_id, room * 2)
    edge_kinds = (wanted or set(LINK_KINDS)) - {SEMANTIC}
    if nearest and edge_kinds:
        ids = [d for d, _ in nearest]
        linked = set(
            session.scalars(
                select(Link.dst_document).where(
                    Link.src_document == document_id,
                    Link.dst_document.in_(ids),
                    Link.status != "rejected",
                    Link.kind.in_(edge_kinds),
                )
            )
        ) | set(
            session.scalars(
                select(Link.src_document).where(
                    Link.dst_document == document_id,
                    Link.src_document.in_(ids),
                    Link.status != "rejected",
                    Link.kind.in_(edge_kinds),
                )
            )
        )
        nearest = [(d, sim) for d, sim in nearest if d not in linked]
    nearest = nearest[:room]
    if not nearest:
        return []
    rows = {
        row.id: row
        for row in session.scalars(
            select(DocumentRow).where(DocumentRow.id.in_([d for d, _ in nearest]))
        )
    }
    return [
        NeighborInfo(
            document_id=d,
            title=rows[d].title,
            source_id=rows[d].source_id,
            language=rows[d].language,
            kind=SEMANTIC,
            score=sim,
            status="computed",
            model_id=model_id,
        )
        for d, sim in nearest
        if d in rows
    ]


# --- curadoria (secção 11.2) ----------------------------------------------------------------------


class LinkView(BaseModel):
    id: int
    src_document: str
    dst_document: str
    src_title: str
    dst_title: str
    src_source: str
    dst_source: str
    kind: str
    score: float | None
    model_id: str | None
    status: str
    created_by: str | None
    created_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None


def _views(session: Session, *conditions: Any, limit: int = 1) -> list[LinkView]:
    src, dst = aliased(DocumentRow), aliased(DocumentRow)
    rows = session.execute(
        select(Link, src, dst)
        .join(src, src.id == Link.src_document)
        .join(dst, dst.id == Link.dst_document)
        .where(*conditions)
        .order_by(Link.score.desc().nulls_last(), Link.id)
        .limit(limit)
    )
    return [
        LinkView(
            id=link.id,
            src_document=link.src_document,
            dst_document=link.dst_document,
            src_title=s.title,
            dst_title=d.title,
            src_source=s.source_id,
            dst_source=d.source_id,
            kind=link.kind,
            score=link.score,
            model_id=link.model_id,
            status=link.status,
            created_by=link.created_by,
            created_at=link.created_at,
            reviewed_by=link.reviewed_by,
            reviewed_at=link.reviewed_at,
        )
        for link, s, d in rows
    ]


class LinksPage(BaseModel):
    items: list[LinkView]
    next_cursor: str | None  # passa-o em `cursor` para a página seguinte; `None` na última


def _parse_cursor(cursor: str) -> tuple[float | None, int]:
    score, _, link_id = cursor.partition(":")
    try:
        return (None if score == "n" else float(score)), int(link_id)
    except ValueError as exc:
        raise LinkError(f"Invalid cursor: {cursor!r}.") from exc


def list_links(
    session: Session,
    *,
    status: Status | None = None,
    kind: str | None = None,
    document_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> LinksPage:
    """As arestas por estado, tipo e documento, das mais para as menos pontuadas (a fila de
    curadoria), paginadas por cursor (keyset sobre `score DESC NULLS LAST, id`)."""
    conditions: list[Any] = []
    if status is not None:
        conditions.append(Link.status == status)
    if kind is not None:
        conditions.append(Link.kind == kind)
    if document_id is not None:
        conditions.append(or_(Link.src_document == document_id, Link.dst_document == document_id))
    if cursor is not None:
        last_score, last_id = _parse_cursor(cursor)
        if last_score is None:
            conditions.append(Link.score.is_(None) & (Link.id > last_id))
        else:
            score = cast(last_score, REAL)  # o mesmo float4 que a coluna guarda
            conditions.append(
                (Link.score < score)
                | ((Link.score == score) & (Link.id > last_id))
                | Link.score.is_(None)
            )
    limit = max(1, min(limit, MAX_LIST))
    views = _views(session, *conditions, limit=limit + 1)
    page, more = views[:limit], len(views) > limit
    last = page[-1] if page else None
    next_cursor = None
    if more and last is not None:
        next_cursor = f"{'n' if last.score is None else repr(last.score)}:{last.id}"
    return LinksPage(items=page, next_cursor=next_cursor)


def create_manual_link(
    session: Session, creator: str, reviewer: str, src_document: str, dst_document: str
) -> tuple[LinkView, bool]:
    """Liga à mão dois documentos (aceite logo). Se a ligação manual já existe (qualquer estado)
    fica aceite outra vez. Devolve a aresta e se foi criada agora. Não faz commit."""
    src_document, dst_document = clean_text(src_document), clean_text(dst_document)
    if src_document == dst_document:
        raise LinkError("A link must connect two different documents.")
    known = set(
        session.scalars(
            select(DocumentRow.id).where(DocumentRow.id.in_([src_document, dst_document]))
        )
    )
    for document_id in (src_document, dst_document):
        if document_id not in known:
            raise DocumentNotFound(
                f"Unknown document: {document_id!r}. Only documents already seen can be linked "
                "(search/read)."
            )
    src, dst = canonical(src_document, dst_document)
    created = session.scalar(
        insert(Link)
        .values(
            src_document=src,
            dst_document=dst,
            kind=MANUAL,
            score=1.0,
            status="accepted",
            created_by=creator,
        )
        .on_conflict_do_nothing(index_elements=["src_document", "dst_document", "kind"])
        .returning(Link.id)
    )
    if created is None:  # já existia: se estava rejeitada ou proposta, passa a aceite
        created_id = session.scalars(
            update(Link)
            .where(
                Link.src_document == src,
                Link.dst_document == dst,
                Link.kind == MANUAL,
                Link.status != "accepted",
            )
            .values(status="accepted", reviewed_by=reviewer, reviewed_at=func.now())
            .returning(Link.id)
        ).first()
        link_id = created_id or session.scalar(
            select(Link.id).where(
                Link.src_document == src, Link.dst_document == dst, Link.kind == MANUAL
            )
        )
    else:
        link_id = created
    assert link_id is not None
    enqueue_refresh(session, [src, dst])  # as páginas do Wiki.js mostram as ligações aceites
    return _views(session, Link.id == link_id)[0], created is not None


def review_link(session: Session, link_id: int, reviewer: str, status: Status) -> LinkView:
    """Aceita, rejeita ou repõe como proposta uma aresta. Não faz commit."""
    updated = session.scalar(
        update(Link)
        .where(Link.id == link_id)
        .values(status=status, reviewed_by=reviewer, reviewed_at=func.now())
        .returning(Link.id)
    )
    if updated is None:
        raise LinkNotFound(f"Link {link_id} does not exist.")
    view = _views(session, Link.id == link_id)[0]
    enqueue_refresh(session, [view.src_document, view.dst_document])
    return view
