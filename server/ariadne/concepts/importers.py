"""Importadores offline das ligações entre línguas (ARCHITECTURE.md, secção 8).

- `import_sitelinks`: o dump `wikidatawiki-latest-wb_items_per_site.sql.gz` (extrato de *sitelinks*
  do Wikidata). Cada item do Wikidata (Q…) é um conceito; os seus artigos nos sites das fontes
  presentes ficam ligados a ele.
- `import_langlinks`: os dumps `page` e `langlinks` de uma Wikipédia (o «caminho exato» sem o dump
  completo do Wikidata): os artigos dessa Wikipédia e as versões noutras línguas da biblioteca.

Só entram os artigos que **existem nos ZIM** das fontes presentes (procurados por título, sem ler o
conteúdo): o resto do dump é lido e ignorado. Tudo em lotes e idempotente.
"""

from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session, sessionmaker

from ariadne.concepts.dump import DumpTable, open_dump
from ariadne.concepts.store import Group, Member, WriteReport, by_site, write_groups
from ariadne.sources.base import Source
from ariadne.wiki.refresh import enqueue_refresh

BATCH = 500
ITEM_COLUMNS = ["ips_row_id", "ips_item_id", "ips_site_id", "ips_site_page"]
PAGE_COLUMNS = ["page_id", "page_namespace", "page_title", "page_is_redirect"]
LANGLINK_COLUMNS = ["ll_from", "ll_lang", "ll_title"]
PROGRESS_EVERY = 5_000_000
MAX_ITEMS = 300_000  # itens resolvidos em memória por passagem (sitelinks)

Progress = Callable[[str], None]


class ConceptImportError(Exception):
    """Um pedido de importação que não faz sentido (site sem fonte, ficheiro que não serve)."""


@dataclass
class ImportReport:
    rows: int = 0  # linhas lidas do dump
    relevant: int = 0  # as dos sites das fontes presentes
    resolved: int = 0  # as que existem num ZIM
    not_in_library: int = 0  # relevantes mas sem artigo no ZIM
    groups: int = 0
    singletons_skipped: int = 0
    ambiguous: int = 0  # ligações `langlinks` ignoradas: juntavam dois artigos da mesma língua
    passes: int = 0  # passagens de resolução ao dump (memória limitada)
    bad_rows: int = 0
    truncated: bool = False
    written: WriteReport = field(default_factory=WriteReport)

    def lines(self) -> list[str]:
        w = self.written
        lines = [
            f"{self.rows} rows read, {self.relevant} from the sources' sites, "
            f"{self.resolved} with an article in the ZIMs ({self.not_in_library} without)",
            f"{self.groups} concepts with 2+ versions ({self.singletons_skipped} with only one, "
            f"ignored)",
            f"{w.concepts_created} new concepts, {w.concepts_merged} merged; "
            f"{w.documents_created} new documents, {w.documents_linked} linked to a concept; "
            f"{w.edges_created} new edges",
        ]
        if w.conflicts:
            lines.append(
                f"{w.conflicts} conflicting documents (from another QID, or in two items of "
                "the same batch) were left as they were, with no new edges"
            )
        if self.ambiguous:
            lines.append(
                f"{self.ambiguous} links ignored: they join two articles of the same language"
            )
        if self.bad_rows or self.truncated:
            lines.append(
                f"WARNING: {self.bad_rows} unreadable rows"
                + ("; the file is truncated (incomplete download?)" if self.truncated else "")
            )
        return lines


def _resolve(sources: Sequence[Source], title: str) -> list[Member]:
    found = []
    for source in sources:
        match = source.find_by_title(title)
        if match is not None:
            found.append(Member(source, match[0], match[1]))
    return found


def _flush(
    sessions: sessionmaker[Session], groups: list[Group], created_by: str, report: ImportReport
) -> None:
    with sessions() as session:
        written = write_groups(session, groups, created_by=created_by)
        enqueue_refresh(
            session, written.touched
        )  # as páginas do Wiki.js refazem o bloco de línguas
        session.commit()
    report.written.add(written)
    report.written.touched.clear()  # só serve de lote: não se acumula


def _batches(groups: list[Group]) -> list[list[Group]]:
    return [groups[i : i + BATCH] for i in range(0, len(groups), BATCH)]


def import_sitelinks(
    sessions: sessionmaker[Session],
    sources: Sequence[Source],
    path: Path,
    *,
    sites: set[str] | None = None,
    min_members: int = 2,
    max_items: int = MAX_ITEMS,
    progress: Progress | None = None,
) -> ImportReport:
    """Importa o dump `wb_items_per_site`. `sites` restringe aos sites dados (têm de ter fonte).
    `max_items` limita quantos itens se resolvem em memória de cada vez (ver as passagens)."""
    library = by_site(sources)
    if sites is not None:
        missing = sorted(sites - set(library))
        if missing:
            raise ConceptImportError(
                f"No source for the sites: {', '.join(missing)}. "
                f"Sites of the sources present: {', '.join(sorted(library)) or '(none)'}."
            )
        library = {site: srcs for site, srcs in library.items() if site in sites}
    if not library:
        raise ConceptImportError(
            "No source has a Wikidata site (source.meta.site): Wikimedia project ZIMs are needed "
            "(wikipedia_pt_…, wikiquote_en_…); run `ariadne sources sync`."
        )
    report = ImportReport()
    # Passagem 1: só conta, por item do Wikidata, em quantos sites das fontes aparece (8 bytes por
    # linha relevante, sem guardar títulos). Só os itens com `min_members` ou mais seguem.
    seen = array("q")
    with open_dump(path) as stream:
        table = DumpTable(stream, "wb_items_per_site", ITEM_COLUMNS)
        item_at, site_at, page_at = (
            table.index("ips_item_id"),
            table.index("ips_site_id"),
            table.index("ips_site_page"),
        )
        for row in table.rows():
            report.rows += 1
            if progress and report.rows % PROGRESS_EVERY == 0:
                progress(f"  {report.rows} linhas lidas…")
            if row[site_at] in library:
                report.relevant += 1
                seen.append(int(row[item_at]))
        report.bad_rows, report.truncated = table.stats.bad_rows, table.stats.truncated
    items_seen, counts = np.unique(np.frombuffer(seen, dtype=np.int64), return_counts=True)
    del seen
    qualifying = items_seen[counts >= max(1, min_members)]
    report.singletons_skipped = int(len(items_seen) - len(qualifying))
    # Passagens seguintes: resolvem-se os títulos nos ZIM só dos itens que qualificam, em fatias
    # de no máximo `max_items` itens (por resto da divisão do id), para a memória ficar limitada
    # mesmo com a Wikipédia inteira: cada fatia é uma leitura do dump e uma série de lotes.
    passes = max(1, -(-len(qualifying) // max(1, max_items)))
    for remainder in range(passes if len(qualifying) else 0):
        wanted = set(qualifying[qualifying % passes == remainder].tolist())
        report.passes += 1
        if progress and passes > 1:
            progress(f"  passagem {remainder + 1}/{passes}: {len(wanted)} conceitos")
        items: dict[int, list[Member]] = {}
        with open_dump(path) as stream:
            table = DumpTable(stream, "wb_items_per_site", ITEM_COLUMNS)
            for row in table.rows():
                if int(row[item_at]) not in wanted or row[site_at] not in library:
                    continue
                found = _resolve(library[row[site_at]], str(row[page_at]))
                if not found:
                    report.not_in_library += 1
                    continue
                report.resolved += 1
                items.setdefault(int(row[item_at]), []).extend(found)
        groups: list[Group] = []
        for item_id in sorted(items):
            members = list({m.document_id: m for m in items[item_id]}.values())
            if len(members) < min_members:
                report.singletons_skipped += 1
                continue
            groups.append(Group(members, qid=f"Q{item_id}"))
        report.groups += len(groups)
        del items
        for batch in _batches(groups):
            _flush(sessions, batch, "sitelinks", report)
    return report


def import_langlinks(
    sessions: sessionmaker[Session],
    sources: Sequence[Source],
    page_path: Path,
    langlinks_path: Path,
    *,
    site: str,
    progress: Progress | None = None,
) -> ImportReport:
    """Importa os dumps `page` e `langlinks` de uma Wikipédia (`site`, ex.: `ptwiki`): cada artigo
    dela com versão noutra língua da biblioteca (mesmo projeto) liga-se a essa versão."""
    library = by_site(sources)
    own = library.get(site)
    if not own:
        raise ConceptImportError(
            f"No source for the site {site!r}. Sites of the sources present: "
            f"{', '.join(sorted(library)) or '(none)'}."
        )
    project = str(own[0].meta.get("project"))
    own_language = str(own[0].meta.get("wiki_language"))
    others: dict[str, list[Source]] = {}
    for source in sources:
        language = str(source.meta.get("wiki_language") or "")
        if source.meta.get("project") == project and language and language != own_language:
            others.setdefault(language, []).append(source)
    if not others:
        raise ConceptImportError(
            f"There are no sources of project {project!r} in languages other than {own_language!r}."
        )
    report = ImportReport()
    pages: dict[int, list[Member]] = {}
    with open_dump(page_path) as stream:
        table = DumpTable(stream, "page", PAGE_COLUMNS)
        id_at, ns_at, title_at, redirect_at = (
            table.index("page_id"),
            table.index("page_namespace"),
            table.index("page_title"),
            table.index("page_is_redirect"),
        )
        for row in table.rows():
            report.rows += 1
            if row[ns_at] != 0 or row[redirect_at]:
                continue
            report.relevant += 1
            found = _resolve(own, str(row[title_at]).replace("_", " "))
            if found:
                pages[int(row[id_at])] = found
        report.bad_rows += table.stats.bad_rows
        report.truncated = report.truncated or table.stats.truncated
    links: dict[tuple[str, str], tuple[Member, Member]] = {}
    with open_dump(langlinks_path) as stream:
        table = DumpTable(stream, "langlinks", LANGLINK_COLUMNS)
        from_at, lang_at, title_at = (
            table.index("ll_from"),
            table.index("ll_lang"),
            table.index("ll_title"),
        )
        for row in table.rows():
            report.rows += 1
            if progress and report.rows % PROGRESS_EVERY == 0:
                progress(f"  {report.rows} rows, {len(links)} links so far…")
            mine = pages.get(int(row[from_at]))
            targets = others.get(str(row[lang_at]).casefold())
            if mine is None or targets is None:
                continue
            report.relevant += 1
            found = _resolve(targets, str(row[title_at]))
            if not found:
                report.not_in_library += 1
                continue
            report.resolved += 1
            for a in mine:
                for b in found:
                    links[(a.document_id, b.document_id)] = (a, b)
        report.bad_rows += table.stats.bad_rows
        report.truncated = report.truncated or table.stats.truncated
    groups, report.ambiguous = _components(list(links.values()))
    report.groups = len(groups)
    for batch in _batches(groups):
        _flush(sessions, batch, "langlinks", report)
    return report


def _components(pairs: list[tuple[Member, Member]]) -> tuple[list[Group], int]:
    """Agrupa as ligações em conceitos (componentes ligados), cada um com as suas arestas. Uma
    ligação que juntaria dois artigos **diferentes da mesma língua/site** (dois artigos PT que
    apontam para o mesmo EN) é ambígua: ignora-se, não se funde nada. Devolve os grupos e quantas
    ligações se ignoraram."""
    parent: dict[str, str] = {}
    members: dict[str, Member] = {}
    sites: dict[str, dict[str, str]] = {}  # raiz -> {site: título} dos seus membros

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def site_of(member: Member) -> str:
        return str(member.source.meta.get("site") or member.source.id)

    ambiguous = 0
    accepted: list[tuple[Member, Member]] = []
    for a, b in sorted(pairs, key=lambda p: (p[0].document_id, p[1].document_id)):
        for m in (a, b):
            if m.document_id not in parent:
                members[m.document_id] = m
                parent[m.document_id] = m.document_id
                sites[m.document_id] = {site_of(m): m.title}
        root_a, root_b = find(a.document_id), find(b.document_id)
        if root_a != root_b:
            clash = any(
                site in sites[root_b] and sites[root_b][site] != title
                for site, title in sites[root_a].items()
            )
            if clash:
                ambiguous += 1
                continue
            parent[root_a] = root_b
            sites[root_b].update(sites.pop(root_a))
        accepted.append((a, b))
    grouped: dict[str, Group] = {}
    for a, b in accepted:
        for member in (a, b):
            group = grouped.setdefault(find(member.document_id), Group([], pairs=[]))
            if member.document_id not in {m.document_id for m in group.members}:
                group.members.append(member)
    for a, b in accepted:
        pair_list = grouped[find(a.document_id)].pairs
        assert pair_list is not None
        pair_list.append((a, b))
    groups = sorted(grouped.values(), key=lambda g: min(m.document_id for m in g.members))
    return groups, ambiguous
