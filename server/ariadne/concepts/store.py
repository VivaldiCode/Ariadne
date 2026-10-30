"""Escrita dos conceitos: documentos, `concept` e arestas entre línguas (ARCHITECTURE.md, secção 8).

Quem importa (sitelinks do Wikidata, dumps `langlinks`) só descobre **grupos**: documentos de fontes
diferentes que são o mesmo tema. Aqui um grupo vira (em lotes, de forma idempotente):

- as linhas `document` que ainda não existem (a partir do ZIM, sem ler o conteúdo);
- um `concept` (com o QID do Wikidata, quando há) e o `document.concept_id` de cada membro;
- arestas `link` aceites (`langlink`: mesmo projeto, línguas diferentes; `wikidata`: projetos
  diferentes), não dirigidas e sem tocar nas que já existem (uma rejeitada na curadoria não volta).

Um conceito com QID manda: um conceito sem QID (de um dump `langlinks`) que ele engloba é fundido
nele, mas um documento que já pertence a *outro* QID não é movido (conflito, contado).
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import Boolean, bindparam, delete, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne.db.models import Concept, Link
from ariadne.db.models import Document as DocumentRow
from ariadne.documents import DocumentInfo, make_document_id, upsert_documents
from ariadne.graph import canonical
from ariadne.sources.base import Source

LANGLINK, WIKIDATA = "langlink", "wikidata"
SCORE = 1.0
EDGE_CHUNK = 2000


@dataclass(frozen=True, slots=True)
class Member:
    """Um artigo de uma fonte que pertence a um grupo."""

    source: Source
    path: str
    title: str

    @property
    def document_id(self) -> str:
        return make_document_id(self.source.id, self.path)

    @property
    def project(self) -> str | None:
        return _meta(self.source, "project")

    @property
    def wiki_language(self) -> str | None:
        return _meta(self.source, "wiki_language")


def _meta(source: Source, key: str) -> str | None:
    value = source.meta.get(key)
    return str(value) if value else None


@dataclass
class Group:
    """Documentos que são o mesmo tema. `qid` quando o Wikidata o diz; `pairs` são as ligações
    explícitas (dumps `langlinks`); sem elas ligam-se todos os pares que o projeto permite."""

    members: list[Member]
    qid: str | None = None
    pairs: list[tuple[Member, Member]] | None = None


@dataclass
class WriteReport:
    documents_created: int = 0
    documents_linked: int = 0  # `concept_id` posto ou mudado
    concepts_created: int = 0
    concepts_merged: int = 0  # conceitos sem QID fundidos noutro
    conflicts: int = 0  # documentos que já são de outro QID: ficam onde estavam
    edges_created: int = 0
    touched: set[str] = field(default_factory=set)  # documentos cujo conceito ou arestas mudaram
    repeated: set[str] = field(default_factory=set)  # em mais de um grupo do lote: ambíguos

    def add(self, other: "WriteReport") -> None:
        self.documents_created += other.documents_created
        self.documents_linked += other.documents_linked
        self.concepts_created += other.concepts_created
        self.concepts_merged += other.concepts_merged
        self.conflicts += other.conflicts
        self.edges_created += other.edges_created
        self.touched |= other.touched


def edge_kind(a: Member, b: Member) -> str | None:
    """O tipo de aresta entre dois membros do mesmo conceito: `langlink` (mesmo projeto, outra
    língua), `wikidata` (outro projeto); nenhuma se é o mesmo site (duas variantes do mesmo ZIM)."""
    if a.document_id == b.document_id:
        return None
    if a.project and a.project == b.project:
        return LANGLINK if a.wiki_language != b.wiki_language else None
    return WIKIDATA


# Chave do `pg_advisory_xact_lock` que serializa as escritas de conceitos (importações e fusões
# a correr em simultâneo, de processos diferentes, não se atropelam). Dura até ao commit do lote.
LOCK_KEY = 0x4152_4941_0007


def write_groups(session: Session, groups: Sequence[Group], *, created_by: str) -> WriteReport:
    """Grava um lote de grupos numa transação (o commit é de quem chama)."""
    report = WriteReport()
    session.execute(select(func.pg_advisory_xact_lock(LOCK_KEY)))
    groups = _without_repeated(groups, report)
    if not groups:
        return report
    members = {m.document_id: m for g in groups for m in g.members}
    existing = set(session.scalars(select(DocumentRow.id).where(DocumentRow.id.in_(list(members)))))
    upsert_documents(session, [DocumentInfo(m.source, m.path, m.title) for m in members.values()])
    report.documents_created = len(set(members) - existing)

    current = _current_concepts(session, list(members))
    targets = _targets(session, groups, current, report)
    in_conflict = _assign(session, groups, targets, current, report)
    _edges(session, groups, created_by, report, in_conflict | report.repeated)
    return report


def _without_repeated(groups: Sequence[Group], report: WriteReport) -> list[Group]:
    """Um documento em mais de um grupo do mesmo lote (dois itens do Wikidata que dão no mesmo
    artigo, p. ex. por um redirect) é ambíguo: sai de todos os grupos, conta como conflito e não
    ganha conceito nem arestas neste lote."""
    owners: dict[str, int] = {}
    for group in groups:
        for document_id in {m.document_id for m in group.members}:
            owners[document_id] = owners.get(document_id, 0) + 1
            if owners[document_id] == 2:
                report.repeated.add(document_id)
    report.conflicts += len(report.repeated)
    kept: list[Group] = []
    for group in groups:
        members = [m for m in group.members if m.document_id not in report.repeated]
        if not members:
            continue
        pairs = group.pairs
        if pairs is not None:
            pairs = [
                (a, b)
                for a, b in pairs
                if a.document_id not in report.repeated and b.document_id not in report.repeated
            ]
        kept.append(Group(members, qid=group.qid, pairs=pairs))
    return kept


# --- conceitos -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Current:
    concept_id: int | None
    qid: str | None


def _current_concepts(session: Session, document_ids: list[str]) -> dict[str, _Current]:
    rows = session.execute(
        select(DocumentRow.id, DocumentRow.concept_id, Concept.wikidata_qid)
        .outerjoin(Concept, Concept.id == DocumentRow.concept_id)
        .where(DocumentRow.id.in_(document_ids))
    )
    return {document_id: _Current(cid, qid) for document_id, cid, qid in rows}


def _label(group: Group) -> str:
    return sorted(group.members, key=lambda m: m.source.id)[0].title


def _targets(
    session: Session, groups: Sequence[Group], current: dict[str, _Current], report: WriteReport
) -> list[int]:
    """O `concept.id` de cada grupo (pela ordem de `groups`); cria os que faltam."""
    result: list[int | None] = [None] * len(groups)
    by_qid: dict[str, str] = {}  # QID -> etiqueta
    for group in groups:
        if group.qid:
            by_qid.setdefault(group.qid, _label(group))
    qid_ids: dict[str, int] = {}
    if by_qid:
        upsert = insert(Concept).values(
            [{"wikidata_qid": q, "label": label} for q, label in by_qid.items()]
        )
        upsert = upsert.on_conflict_do_update(
            index_elements=[Concept.wikidata_qid],
            set_={"label": func.coalesce(Concept.label, upsert.excluded.label)},
        )
        inserted_flag = literal_column("(xmax = 0)", Boolean).label("inserted")
        for concept_id, qid, inserted in session.execute(
            upsert.returning(Concept.id, Concept.wikidata_qid, inserted_flag)
        ):
            qid_ids[str(qid)] = concept_id
            report.concepts_created += 1 if inserted else 0
    new_labels: list[tuple[int, str]] = []
    for index, group in enumerate(groups):
        if group.qid:
            result[index] = qid_ids[group.qid]
            continue
        known = [current[m.document_id] for m in group.members if m.document_id in current]
        known = [c for c in known if c.concept_id is not None]
        with_qid = sorted(c.concept_id for c in known if c.qid and c.concept_id is not None)
        plain = sorted(c.concept_id for c in known if c.concept_id is not None)
        if with_qid:
            result[index] = with_qid[0]
        elif plain:
            result[index] = plain[0]
        else:
            new_labels.append((index, _label(group)))
    if new_labels:
        ids = session.scalars(
            insert(Concept).returning(Concept.id, sort_by_parameter_order=True),
            [{"label": label} for _, label in new_labels],
        ).all()
        report.concepts_created += len(ids)
        for (index, _), concept_id in zip(new_labels, ids, strict=True):
            result[index] = concept_id
    return [int(r) for r in result if r is not None]


def _assign(
    session: Session,
    groups: Sequence[Group],
    targets: list[int],
    current: dict[str, _Current],
    report: WriteReport,
) -> set[str]:
    """Põe `document.concept_id` e funde os conceitos sem QID que ficaram para trás. Devolve os
    documentos em conflito (já são de outro QID): ficam como estavam e sem arestas novas."""
    updates: dict[str, int] = {}
    conflicts: set[str] = set()
    old: dict[int, set[int]] = {}  # conceito sem QID substituído -> os destinos dos seus membros
    for group, target in zip(groups, targets, strict=True):
        for member in group.members:
            now = current[member.document_id]
            if now.concept_id == target:
                continue
            if now.concept_id is not None and now.qid is not None:
                conflicts.add(member.document_id)  # o Wikidata decide, não se mexe
                continue
            if now.concept_id is not None:
                old.setdefault(now.concept_id, set()).add(target)
            updates[member.document_id] = target
    report.conflicts += len(conflicts)
    if updates:
        session.connection().execute(
            update(DocumentRow)
            .where(DocumentRow.id == bindparam("b_id"))
            .values(concept_id=bindparam("b_concept")),
            [{"b_id": d, "b_concept": c} for d, c in updates.items()],
        )
        report.documents_linked = len(updates)
        report.touched |= set(updates)
    for source_id, destinations in old.items():
        if len(destinations) != 1:
            continue  # membros para conceitos diferentes: não há destino único para o resto
        (target,) = destinations
        if source_id == target:
            continue
        moved = session.scalars(
            update(DocumentRow)
            .where(DocumentRow.concept_id == source_id)
            .values(concept_id=target)
            .returning(DocumentRow.id)
        ).all()
        report.touched |= set(moved)
        _carry_origin(session, source_id, target)
        session.execute(delete(Concept).where(Concept.id == source_id))
        report.concepts_merged += 1
    return conflicts


def _carry_origin(session: Session, source_id: int, target_id: int) -> None:
    """A língua original do conceito que desaparece passa para o que fica, se este não tem."""
    origin = session.execute(
        select(Concept.origin_lang, Concept.origin_source).where(Concept.id == source_id)
    ).one_or_none()
    if origin is None or origin[0] is None:
        return
    session.execute(
        update(Concept)
        .where(Concept.id == target_id, Concept.origin_lang.is_(None))
        .values(origin_lang=origin[0], origin_source=origin[1])
    )


# --- arestas ---------------------------------------------------------------------------------


def _edges(
    session: Session,
    groups: Sequence[Group],
    created_by: str,
    report: WriteReport,
    skip: set[str],
) -> None:
    rows: dict[tuple[str, str, str], dict[str, object]] = {}
    for group in groups:
        unique = list({m.document_id: m for m in group.members}.values())
        pairs = group.pairs if group.pairs is not None else list(combinations(unique, 2))
        for a, b in pairs:
            kind = edge_kind(a, b)
            if kind is None or a.document_id in skip or b.document_id in skip:
                continue
            src, dst = canonical(a.document_id, b.document_id)
            rows[(src, dst, kind)] = {
                "src_document": src,
                "dst_document": dst,
                "kind": kind,
                "score": SCORE,
                "status": "accepted",
                "created_by": created_by,
            }
    ordered = [rows[key] for key in sorted(rows)]
    for start in range(0, len(ordered), EDGE_CHUNK):
        stmt = (
            insert(Link)
            .values(ordered[start : start + EDGE_CHUNK])
            .on_conflict_do_nothing(index_elements=["src_document", "dst_document", "kind"])
            .returning(Link.src_document, Link.dst_document)
        )
        for src, dst in session.execute(stmt):
            report.edges_created += 1
            report.touched |= {src, dst}


def by_site(sources: Sequence[Source]) -> dict[str, list[Source]]:
    """site do Wikidata -> fontes que o têm (`source.meta.site`), por id."""
    sites: dict[str, list[Source]] = defaultdict(list)
    for source in sorted(sources, key=lambda s: s.id):
        if site := _meta(source, "site"):
            sites[site].append(source)
    return dict(sites)
