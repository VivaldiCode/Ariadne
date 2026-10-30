"""Fase 7: conceitos e ligações entre línguas (sitelinks do Wikidata e dumps `langlinks`)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from ariadne.concepts.importers import ConceptImportError, import_langlinks, import_sitelinks
from ariadne.concepts.store import Group, Member, WriteReport, write_groups
from ariadne.db.models import Concept, Job, Link
from ariadne.db.models import Document as DocumentRow
from ariadne.sources.base import Source
from ariadne.sources.registry import SourceRegistry
from tests import sitelinks
from tests.langzims import (
    BOOKS,
    BOOKS_CRAVOS,
    EN,
    EN_CRAVOS,
    EN_LISBON,
    EN_TRICKY,
    PT,
    PT_CRAVOS,
    PT_LISBOA,
    PT_TRICKY,
    make_lang_dir,
)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]


@pytest.fixture
def lang_sources(db: sessionmaker[Session], tmp_path: Path) -> Iterator[list[Source]]:
    registry = SourceRegistry(make_lang_dir(tmp_path / "zim"))
    with db() as session:
        registry.sync(session)
    yield registry.active()
    registry.close()


@pytest.fixture
def items_dump(tmp_path: Path) -> Path:
    return sitelinks.write_items_dump(tmp_path / "wb_items_per_site.sql.gz")


def concept_of(db: sessionmaker[Session], document_id: str) -> Concept | None:
    with db() as session:
        document = session.get(DocumentRow, document_id)
        if document is None or document.concept_id is None:
            return None
        return session.get(Concept, document.concept_id)


def edges(db: sessionmaker[Session], kind: str | None = None) -> dict[tuple[str, str], Link]:
    with db() as session:
        query = select(Link).order_by(Link.id)
        if kind:
            query = query.where(Link.kind == kind)
        return {(link.src_document, link.dst_document): link for link in session.scalars(query)}


def test_as_fontes_ganham_projeto_e_site(lang_sources: list[Source]) -> None:
    sites = {s.id: s.meta["site"] for s in lang_sources}
    assert sites == {PT: "ptwiki", EN: "enwiki", BOOKS: "ptwikibooks"}


# --- sitelinks do Wikidata ---------------------------------------------------------------------


def test_sitelinks_criam_conceitos_documentos_e_arestas(
    db: sessionmaker[Session], lang_sources: list[Source], items_dump: Path
) -> None:
    report = import_sitelinks(db, lang_sources, items_dump)

    cravos = concept_of(db, PT_CRAVOS)
    assert cravos is not None
    assert cravos.wikidata_qid == f"Q{sitelinks.Q_CRAVOS}"
    assert cravos.label == "Revolução dos Cravos"
    assert cravos.origin_lang is None  # sem dados, fica NULL: não se inventa
    for document_id in (EN_CRAVOS, BOOKS_CRAVOS):
        other = concept_of(db, document_id)
        assert other is not None and other.id == cravos.id
    lisboa, lisbon = concept_of(db, PT_LISBOA), concept_of(db, EN_LISBON)
    assert lisboa is not None and lisboa.wikidata_qid == "Q77"
    assert lisbon is not None and lisbon.id == lisboa.id
    # O título difícil (aspas, barra, parênteses, vírgula, ponto e vírgula) bate com o do ZIM.
    tricky, tricky_en = concept_of(db, PT_TRICKY), concept_of(db, EN_TRICKY)
    assert tricky is not None and tricky.wikidata_qid == f"Q{sitelinks.Q_DIFICIL}"
    assert tricky_en is not None and tricky_en.id == tricky.id

    assert report.groups == 3
    assert report.written.concepts_created == 3
    assert report.written.documents_created == 7  # criados a partir do ZIM, sem ler o conteúdo
    langlinks = edges(db, "langlink")
    assert set(langlinks) == {
        tuple(sorted(pair))
        for pair in ((PT_CRAVOS, EN_CRAVOS), (PT_LISBOA, EN_LISBON), (PT_TRICKY, EN_TRICKY))
    }
    wikidata = edges(db, "wikidata")  # projetos diferentes (Wikipédia ↔ Wikilivros), mesmo QID
    assert set(wikidata) == {
        tuple(sorted((PT_CRAVOS, BOOKS_CRAVOS))),
        tuple(sorted((EN_CRAVOS, BOOKS_CRAVOS))),
    }
    for src_dst, link in {**langlinks, **wikidata}.items():
        assert src_dst[0] < src_dst[1]  # não dirigida: src < dst
        assert (link.status, link.score, link.created_by) == ("accepted", 1.0, "sitelinks")


def test_sitelinks_so_dos_sites_pedidos(
    db: sessionmaker[Session], lang_sources: list[Source], items_dump: Path
) -> None:
    import_sitelinks(db, lang_sources, items_dump, sites={"ptwiki", "enwiki"})

    assert edges(db, "wikidata") == {}  # sem Wikilivros
    assert concept_of(db, PT_CRAVOS) is not None
    with db() as session:
        assert session.get(DocumentRow, BOOKS_CRAVOS) is None

    with pytest.raises(ConceptImportError, match="No source for the sites: dewiki"):
        import_sitelinks(db, lang_sources, items_dump, sites={"ptwiki", "dewiki"})


def test_sitelinks_idempotente_e_respeita_rejeitadas(
    db: sessionmaker[Session], lang_sources: list[Source], items_dump: Path
) -> None:
    import_sitelinks(db, lang_sources, items_dump)
    before = {k: v.id for k, v in edges(db).items()}
    with db() as session, session.begin():
        session.execute(
            update(Link).where(Link.kind == "langlink").values(status="rejected", reviewed_by="1")
        )

    again = import_sitelinks(db, lang_sources, items_dump)

    assert again.written.edges_created == 0
    assert again.written.concepts_created == 0
    assert again.written.documents_created == 0
    assert again.written.documents_linked == 0
    assert {k: v.id for k, v in edges(db).items()} == before
    assert all(link.status == "rejected" for link in edges(db, "langlink").values())
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Concept)) == 3


def test_sitelinks_com_dump_cortado_importa_o_que_le(
    db: sessionmaker[Session], lang_sources: list[Source], tmp_path: Path
) -> None:
    text = sitelinks.items_dump()
    cut = text.index("(8,77,'ptwiki'") + 10  # a meio da tupla do Lisboa
    path = sitelinks.write_items_dump(tmp_path / "cortado.sql.gz", text[:cut])

    report = import_sitelinks(db, lang_sources, path)

    assert report.truncated and report.bad_rows == 1
    assert concept_of(db, PT_CRAVOS) is not None
    with db() as session:
        assert session.get(DocumentRow, PT_LISBOA) is None  # a tupla cortada não entrou
    assert any("WARNING" in line and "truncated" in line for line in report.lines())


def test_sitelinks_enfileira_o_refresh_das_paginas_com_wiki(
    db: sessionmaker[Session], lang_sources: list[Source], items_dump: Path
) -> None:
    from ariadne.documents import DocumentInfo, upsert_documents

    pt = next(s for s in lang_sources if s.id == PT)
    with db() as session:
        upsert_documents(
            session, [DocumentInfo(pt, "Revolucao_dos_Cravos", "Revolução dos Cravos")]
        )
        session.execute(
            update(DocumentRow).where(DocumentRow.id == PT_CRAVOS).values(wiki_page_id=7)
        )
        session.commit()

    import_sitelinks(db, lang_sources, items_dump)

    with db() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "import_page")).all()
        assert [j.payload["document_id"] for j in jobs] == [PT_CRAVOS]


# --- dumps page + langlinks de uma Wikipédia ---------------------------------------------------


@pytest.fixture
def ptwiki_dumps(tmp_path: Path) -> tuple[Path, Path]:
    page = tmp_path / "ptwiki-page.sql.gz"
    langlinks = tmp_path / "ptwiki-langlinks.sql.gz"
    sitelinks.write_items_dump(
        page,
        sitelinks.page_dump(
            [
                (1, 0, "Revolução_dos_Cravos", 0),
                (2, 0, "Lisboa", 0),
                (3, 0, "Redirecionamento", 1),
                (4, 1, "Discussão:Lisboa", 0),
                (5, 0, sitelinks.TRICKY_PT.replace(" ", "_"), 0),
            ]
        ),
    )
    sitelinks.write_items_dump(
        langlinks,
        sitelinks.langlinks_dump(
            [
                (1, "en", "Carnation Revolution"),
                (1, "fr", "Révolution des œillets"),
                (2, "en", "Lisbon"),
                (3, "en", "Redirect en"),
                (4, "en", "Talk"),
                (5, "en", "Tricky title"),
                (2, "zz", "x"),
            ]
        ),
    )
    return page, langlinks


def test_langlinks_ligam_as_versoes_sem_qid(
    db: sessionmaker[Session],
    lang_sources: list[Source],
    ptwiki_dumps: tuple[Path, Path],
) -> None:
    report = import_langlinks(db, lang_sources, *ptwiki_dumps, site="ptwiki")

    assert report.groups == 3
    cravos = concept_of(db, PT_CRAVOS)
    assert cravos is not None and cravos.wikidata_qid is None
    en_cravos = concept_of(db, EN_CRAVOS)
    assert en_cravos is not None and en_cravos.id == cravos.id
    assert set(edges(db)) == {
        tuple(sorted(pair))
        for pair in ((PT_CRAVOS, EN_CRAVOS), (PT_LISBOA, EN_LISBON), (PT_TRICKY, EN_TRICKY))
    }
    assert {link.kind for link in edges(db).values()} == {"langlink"}
    assert {link.created_by for link in edges(db).values()} == {"langlinks"}

    again = import_langlinks(db, lang_sources, *ptwiki_dumps, site="ptwiki")
    assert (again.written.edges_created, again.written.concepts_created) == (0, 0)
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Concept)) == 3


def test_sitelinks_depois_de_langlinks_funde_o_conceito_sem_qid(
    db: sessionmaker[Session],
    lang_sources: list[Source],
    ptwiki_dumps: tuple[Path, Path],
    items_dump: Path,
) -> None:
    import_langlinks(db, lang_sources, *ptwiki_dumps, site="ptwiki")
    with db() as session, session.begin():
        concept = session.scalars(
            select(Concept)
            .join(DocumentRow, DocumentRow.concept_id == Concept.id)
            .where(DocumentRow.id == PT_CRAVOS)
        ).one()
        concept.origin_lang, concept.origin_source = "pt", "manual"

    report = import_sitelinks(db, lang_sources, items_dump)

    assert report.written.concepts_merged == 3  # os 3 conceitos sem QID passaram para os dos QID
    cravos = concept_of(db, PT_CRAVOS)
    assert cravos is not None and cravos.wikidata_qid == f"Q{sitelinks.Q_CRAVOS}"
    assert (cravos.origin_lang, cravos.origin_source) == ("pt", "manual")  # herdou a origem
    for document_id in (EN_CRAVOS, BOOKS_CRAVOS):
        other = concept_of(db, document_id)
        assert other is not None and other.id == cravos.id
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Concept)) == 3
        assert (
            session.scalar(
                select(func.count()).select_from(Concept).where(Concept.wikidata_qid.is_(None))
            )
            == 0
        )


def test_langlinks_sem_fonte_do_site(
    db: sessionmaker[Session], lang_sources: list[Source], ptwiki_dumps: tuple[Path, Path]
) -> None:
    with pytest.raises(ConceptImportError, match="No source for the site 'eswiki'"):
        import_langlinks(db, lang_sources, *ptwiki_dumps, site="eswiki")


# --- memória limitada, ambiguidades e conflitos ---------------------------------------------------


def test_sitelinks_em_varias_passagens_da_o_mesmo_resultado(
    db: sessionmaker[Session], lang_sources: list[Source], items_dump: Path
) -> None:
    report = import_sitelinks(db, lang_sources, items_dump, max_items=1)  # 1 conceito por passagem

    assert report.passes == 3 and report.groups == 3
    assert report.written.concepts_created == 3 and report.written.edges_created == 5
    assert report.singletons_skipped == 0  # os itens sem site nas fontes nem chegam a contar
    cravos, lisboa = concept_of(db, PT_CRAVOS), concept_of(db, PT_LISBOA)
    assert cravos is not None and lisboa is not None and cravos.id != lisboa.id
    again = import_sitelinks(db, lang_sources, items_dump, max_items=2)
    assert again.passes == 2 and again.written.edges_created == 0


def test_sitelinks_so_resolve_os_itens_com_mais_de_um_site_presente(
    db: sessionmaker[Session], lang_sources: list[Source], tmp_path: Path
) -> None:
    rows = [
        (1, 10, "ptwiki", "Lisboa"),  # só o ptwiki está na biblioteca neste item
        (2, 10, "dewiki", "Lissabon"),
        (3, 11, "ptwiki", sitelinks.PT_CRAVOS),
        (4, 11, "enwiki", sitelinks.EN_CRAVOS),
    ]
    path = sitelinks.write_items_dump(tmp_path / "poucos.sql.gz", sitelinks.items_dump(rows))

    report = import_sitelinks(db, lang_sources, path)

    assert (report.relevant, report.resolved, report.groups, report.singletons_skipped) == (
        3,
        2,
        1,
        1,
    )
    with db() as session:
        assert session.get(DocumentRow, PT_LISBOA) is None  # nem foi resolvido


def test_dois_artigos_pt_para_o_mesmo_en_nao_se_unem(
    db: sessionmaker[Session], lang_sources: list[Source], tmp_path: Path
) -> None:
    page = sitelinks.write_items_dump(
        tmp_path / "page.sql.gz",
        sitelinks.page_dump([(1, 0, "Revolução_dos_Cravos", 0), (2, 0, "Lisboa", 0)]),
    )
    links = sitelinks.write_items_dump(
        tmp_path / "ll.sql.gz",
        sitelinks.langlinks_dump(
            [(1, "en", "Carnation Revolution"), (2, "en", "Carnation Revolution")]
        ),
    )

    report = import_langlinks(db, lang_sources, page, links, site="ptwiki")

    assert report.ambiguous == 1 and report.groups == 1
    linked = [d for d in (PT_CRAVOS, PT_LISBOA) if concept_of(db, d) is not None]
    assert len(linked) == 1  # só um dos dois PT ficou com o EN; o outro não foi fundido
    assert len(edges(db, "langlink")) == 1
    en = concept_of(db, EN_CRAVOS)
    assert en is not None and en.id == concept_of(db, linked[0]).id  # type: ignore[union-attr]
    assert any("same language" in line for line in report.lines())


def member(sources: list[Source], document_id: str) -> Member:
    source_id, path = document_id.split("/", 1)
    source = next(s for s in sources if s.id == source_id)
    found = source.find_by_title(path.replace("_", " "))
    assert found is not None, document_id
    return Member(source, found[0], found[1])


def write(db: sessionmaker[Session], groups: list[Group]) -> WriteReport:
    with db() as session:
        report = write_groups(session, groups, created_by="teste")
        session.commit()
    return report


def test_documento_de_outro_qid_fica_onde_estava_e_sem_arestas(
    db: sessionmaker[Session], lang_sources: list[Source]
) -> None:
    pt, en, books = (member(lang_sources, d) for d in (PT_CRAVOS, EN_CRAVOS, BOOKS_CRAVOS))
    write(db, [Group([pt, en], qid="Q1")])
    before = set(edges(db))

    report = write(db, [Group([pt, books], qid="Q2")])  # o PT já é de Q1

    assert report.conflicts == 1
    pt_concept, books_concept = concept_of(db, PT_CRAVOS), concept_of(db, BOOKS_CRAVOS)
    assert pt_concept is not None and pt_concept.wikidata_qid == "Q1"  # ficou onde estava
    assert books_concept is not None and books_concept.wikidata_qid == "Q2"
    assert set(edges(db)) == before  # nenhuma aresta entre o PT em conflito e os Wikilivros


def test_documento_repetido_no_mesmo_lote_e_ambiguo(
    db: sessionmaker[Session], lang_sources: list[Source]
) -> None:
    pt, en, books = (member(lang_sources, d) for d in (PT_CRAVOS, EN_CRAVOS, BOOKS_CRAVOS))

    report = write(db, [Group([pt, en], qid="Q1"), Group([pt, books], qid="Q2")])

    assert report.conflicts == 1
    assert concept_of(db, PT_CRAVOS) is None  # ambíguo: não ganha conceito…
    assert edges(db) == {}  # …nem arestas
    assert concept_of(db, EN_CRAVOS) is not None and concept_of(db, BOOKS_CRAVOS) is not None


def test_fusao_nao_manda_tudo_para_o_primeiro_destino(
    db: sessionmaker[Session], lang_sources: list[Source]
) -> None:
    pt, en, books = (member(lang_sources, d) for d in (PT_CRAVOS, EN_CRAVOS, BOOKS_CRAVOS))
    write(db, [Group([pt, en, books], pairs=[(pt, en), (pt, books)])])  # um conceito sem QID
    old = concept_of(db, PT_CRAVOS)
    assert old is not None and old.wikidata_qid is None

    report = write(db, [Group([pt], qid="Q1"), Group([books], qid="Q2")])  # destinos diferentes

    assert report.concepts_merged == 0
    assert concept_of(db, PT_CRAVOS).wikidata_qid == "Q1"  # type: ignore[union-attr]
    assert concept_of(db, BOOKS_CRAVOS).wikidata_qid == "Q2"  # type: ignore[union-attr]
    with db() as session:
        assert session.get(Concept, old.id) is not None  # o antigo ficou (ainda tem o EN)


def test_escritas_de_conceitos_serializam_com_o_lock_consultivo(
    db: sessionmaker[Session], lang_sources: list[Source]
) -> None:
    import threading
    import time

    pt, en = member(lang_sources, PT_CRAVOS), member(lang_sources, EN_CRAVOS)
    done = threading.Event()
    with db() as first:
        write_groups(first, [Group([pt, en], qid="Q1")], created_by="teste")  # segura o lock

        def second() -> None:
            write(db, [Group([pt, en], qid="Q1")])  # outro processo, outra ligação
            done.set()

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.5)
        assert not done.is_set()  # à espera do lock
        first.commit()
        thread.join(10)
    assert done.is_set()
