from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, create_principal
from ariadne.catalog import list_sources
from ariadne.config import Settings
from ariadne.db.models import Document, QueryLog, QueryResult
from ariadne.documents import (
    DocumentNotFound,
    SectionNotFound,
    UnknownSource,
    read_document,
)
from ariadne.kiwix import KiwixClient
from ariadne.search import FeedbackError, SearchError, give_feedback, search
from ariadne.services import Services
from ariadne.sources.registry import SourceRegistry
from tests.fakes import EN_ARTICLES, EN_BOOK, PT_BOOK, FakeKiwix, default_fake
from tests.zimfactory import kiwix_books, make_zim

pytestmark = pytest.mark.db


@pytest.fixture
def fake() -> FakeKiwix:
    return default_fake()


def build_services(sessions: sessionmaker[Session], zim_dir: Path, kiwix: KiwixClient) -> Services:
    settings = Settings(
        database_url="postgresql+psycopg://x@localhost/x",
        zim_dir=zim_dir,
        kiwix_url="http://kiwix",
        kiwix_public_url="http://kiwix.publico:8801",
    )
    registry = SourceRegistry(zim_dir)
    with sessions() as session:
        registry.sync(session)
    return Services(settings, sessions, registry, kiwix)


@pytest.fixture
def services(db: sessionmaker[Session], zim_dir: Path, fake: FakeKiwix) -> Iterator[Services]:
    services = build_services(db, zim_dir, fake.client())
    yield services
    services.close()


@pytest.fixture
def principal_id(db: sessionmaker[Session]) -> int:
    with db() as session, session.begin():
        principal, _ = create_principal(session, PrincipalKind.agent, "agente-de-teste")
        return principal.id


@pytest.fixture
def session(db: sessionmaker[Session]) -> Iterator[Session]:
    with db() as s:
        yield s


# --- pesquisa -----------------------------------------------------------------------------------


def test_search_regista_consulta_resultados_e_documentos(
    services: Services, session: Session, principal_id: int
) -> None:
    result = search(services, session, principal_id, "capital", k=2, session_id="sessao-1")

    assert [h.document_id for h in result.results] == [
        "zim:wikipedia_pt_teste/Lisboa",
        "zim:wikipedia_pt_teste/Portugal",
    ]
    first = result.results[0]
    assert (first.rank, first.level, first.score) == (1, 1, 1.0)
    assert result.results[1].score == 0.5  # 1/(1+posição a contar de zero)
    assert first.title == "Lisboa"
    assert first.source_id == "zim:wikipedia_pt_teste"
    assert first.language == "pt"
    assert first.snippet == "excerto de Lisboa"
    assert first.kiwix_url == f"http://kiwix.publico:8801/content/{PT_BOOK}/Lisboa"
    assert first.wiki_url is None

    log = session.get(QueryLog, result.query_id)
    assert log is not None
    assert (log.principal_id, log.query, log.level_served) == (principal_id, "capital", 1)
    assert (log.top_score, log.session_id, log.query_lang) == (1.0, "sessao-1", None)
    assert log.filters == {"lang": None, "sources": None, "k": 2}
    assert log.latency_ms is not None and log.latency_ms >= 0
    rows = session.scalars(select(QueryResult).order_by(QueryResult.rank)).all()
    assert [(r.query_id, r.rank, r.document_id, r.level, r.score) for r in rows] == [
        (result.query_id, 1, "zim:wikipedia_pt_teste/Lisboa", 1, 1.0),
        (result.query_id, 2, "zim:wikipedia_pt_teste/Portugal", 1, 0.5),
    ]
    document = session.get(Document, "zim:wikipedia_pt_teste/Lisboa")
    assert document is not None
    assert (document.source_id, document.path, document.version) == (
        "zim:wikipedia_pt_teste",
        "Lisboa",
        "2026-09",
    )
    assert (document.title, document.language) == ("Lisboa", "pt")


def test_search_repetida_nao_duplica_documentos(
    services: Services, session: Session, principal_id: int
) -> None:
    first = search(services, session, principal_id, "capital")
    second = search(services, session, principal_id, "capital")

    assert first.query_id != second.query_id
    assert len(session.scalars(select(Document)).all()) == 3
    assert len(session.scalars(select(QueryLog)).all()) == 2


def test_k_fica_entre_1_e_50(
    services: Services, session: Session, principal_id: int, fake: FakeKiwix
) -> None:
    search(services, session, principal_id, "a", k=0)
    search(services, session, principal_id, "a", k=500)
    assert [u.params["pageLength"] for u in fake.requests] == ["1", "50"]


def test_search_vazia(services: Services, session: Session, principal_id: int) -> None:
    with pytest.raises(SearchError, match="empty"):
        search(services, session, principal_id, "   ")


def test_search_filtros(services: Services, session: Session, principal_id: int) -> None:
    for lang in ("pt", "por", "PT-br"):
        assert len(search(services, session, principal_id, "x", lang=lang).results) == 3
    for name in ("zim:wikipedia_pt_teste", "wikipedia_pt_teste"):
        assert len(search(services, session, principal_id, "x", sources=[name]).results) == 3
    with pytest.raises(SearchError, match="Available languages: pt"):
        search(services, session, principal_id, "x", lang="en")
    with pytest.raises(SearchError, match="Available: zim:wikipedia_pt_teste"):
        search(services, session, principal_id, "x", sources=["nao-existe"])
    log = session.scalars(select(QueryLog).order_by(QueryLog.id.desc())).first()
    assert log is not None and log.filters is not None
    assert log.filters["sources"] == ["wikipedia_pt_teste"]  # só as pesquisas feitas ficam


def test_uma_pesquisa_por_lingua_e_fusao(
    services: Services, session: Session, principal_id: int, zim_dir: Path, fake: FakeKiwix
) -> None:
    make_zim(
        zim_dir / "wikipedia_en_teste_2026-09.zim",
        name="wikipedia_en_teste",
        language="eng",
        articles=EN_ARTICLES,
        redirects={},
    )
    result = search(services, session, principal_id, "capital")  # sync_if_changed apanha o novo ZIM

    # O kiwix recusaria os dois livros no mesmo pedido: houve um pedido por língua.
    assert sorted(kiwix_books(u.params)[0] for u in fake.requests) == [EN_BOOK, PT_BOOK]
    ids = [h.document_id for h in result.results]
    # Round-robin por posição, a começar pela língua com mais resultados (pt tem 3, en tem 1).
    assert ids == [
        "zim:wikipedia_pt_teste/Lisboa",
        "zim:wikipedia_en_teste/London",
        "zim:wikipedia_pt_teste/Portugal",
        "zim:wikipedia_pt_teste/Outro",
    ]
    assert [h.rank for h in result.results] == [1, 2, 3, 4]
    assert {h.language for h in result.results} == {"pt", "en"}
    only_en = search(services, session, principal_id, "capital", lang="en")
    assert [h.document_id for h in only_en.results] == ["zim:wikipedia_en_teste/London"]


def test_kiwix_em_baixo(
    services: Services, session: Session, principal_id: int, fake: FakeKiwix
) -> None:
    fake.down.add(PT_BOOK)
    with pytest.raises(SearchError, match="Search unavailable"):
        search(services, session, principal_id, "capital")
    assert session.scalars(select(QueryLog)).all() == []


def test_uma_lingua_falha_e_a_outra_responde(
    services: Services, session: Session, principal_id: int, zim_dir: Path, fake: FakeKiwix
) -> None:
    make_zim(
        zim_dir / "wikipedia_en_teste_2026-09.zim",
        name="wikipedia_en_teste",
        language="eng",
        articles=EN_ARTICLES,
        redirects={},
    )
    fake.down.add(EN_BOOK)
    result = search(services, session, principal_id, "capital")

    assert len(result.results) == 3 and {h.language for h in result.results} == {"pt"}
    assert len(result.warnings) == 1 and "unavailable" in result.warnings[0]


def test_livro_que_o_kiwix_nao_conhece(
    services: Services, session: Session, principal_id: int, zim_dir: Path, fake: FakeKiwix
) -> None:
    make_zim(zim_dir / "wikipedia_pt_nova_2026-09.zim", name="wikipedia_pt_nova")
    fake.unknown.add("wikipedia_pt_nova_2026-09")
    fake.languages["wikipedia_pt_nova_2026-09"] = "por"
    result = search(services, session, principal_id, "capital")

    assert len(result.results) == 3  # o resto pesquisa-se na mesma
    assert "wikipedia_pt_nova_2026-09" in result.warnings[0]


# --- feedback -----------------------------------------------------------------------------------


def test_feedback(
    services: Services, session: Session, principal_id: int, db: sessionmaker[Session]
) -> None:
    result = search(services, session, principal_id, "capital")
    lisboa, portugal = (h.document_id for h in result.results[:2])

    assert give_feedback(session, principal_id, result.query_id, lisboa, True) == 1
    assert give_feedback(session, principal_id, result.query_id, portugal, False) == 1
    session.expire_all()
    rows = {r.document_id: r for r in session.scalars(select(QueryResult))}
    assert (rows[lisboa].feedback, rows[lisboa].used) == (1, True)
    assert (rows[portugal].feedback, rows[portugal].used) == (-1, None)

    with pytest.raises(FeedbackError, match="is not in the results"):
        give_feedback(
            session, principal_id, result.query_id, "zim:wikipedia_pt_teste/Mentira", True
        )
    with pytest.raises(FeedbackError, match="Search 999999 not found"):
        give_feedback(session, principal_id, 999999, lisboa, True)
    with db() as other, other.begin():
        outsider, _ = create_principal(other, PrincipalKind.agent, "intruso")
        outsider_id = outsider.id
    with pytest.raises(FeedbackError, match="not found"):  # a pesquisa é de outro principal
        give_feedback(session, outsider_id, result.query_id, lisboa, True)


# --- read ---------------------------------------------------------------------------------------


def test_read(services: Services, session: Session, principal_id: int) -> None:
    pesquisa = search(services, session, principal_id, "capital", session_id="s1")
    view = read_document(
        services, session, principal_id, "zim:wikipedia_pt_teste/Lisboa", session_id="s1"
    )

    assert view.document_id == "zim:wikipedia_pt_teste/Lisboa"
    assert (view.title, view.language, view.version) == ("Lisboa", "pt", "2026-09")
    assert view.text.startswith("Lisboa é a capital de Portugal")
    assert (view.truncated, view.section) == (False, None)
    assert view.total_chars == len(view.text)
    assert [(s.title, s.level) for s in view.sections] == [
        ("Lisboa", 1),
        ("História", 2),
        ("Terramoto", 3),
        ("Geografia", 2),
    ]
    assert [(link.document_id, link.title) for link in view.links] == [
        ("zim:wikipedia_pt_teste/Portugal", "Portugal")
    ]
    assert view.kiwix_url == f"http://kiwix.publico:8801/content/{PT_BOOK}/Lisboa"
    # Abrir um resultado da pesquisa marca-o como usado.
    session.expire_all()
    used = {r.document_id: r.used for r in session.scalars(select(QueryResult))}
    assert used["zim:wikipedia_pt_teste/Lisboa"] is True
    assert used["zim:wikipedia_pt_teste/Portugal"] is None
    assert pesquisa.query_id


def test_read_nao_marca_usado_noutra_sessao(
    services: Services, session: Session, principal_id: int
) -> None:
    search(services, session, principal_id, "capital", session_id="s1")
    read_document(services, session, principal_id, "zim:wikipedia_pt_teste/Lisboa", session_id="s2")
    session.expire_all()
    assert {r.used for r in session.scalars(select(QueryResult))} == {None}


def test_read_seccao_redirect_e_corte(
    services: Services, session: Session, principal_id: int
) -> None:
    view = read_document(
        services,
        session,
        principal_id,
        "zim:wikipedia_pt_teste/Capital_de_Portugal",
        section="## terramoto",
    )
    assert view.document_id == "zim:wikipedia_pt_teste/Lisboa"  # o canónico
    assert view.redirected_from == "zim:wikipedia_pt_teste/Capital_de_Portugal"
    assert view.section == "Terramoto"
    assert view.text == "### Terramoto\n\nO terramoto de 1755 destruiu grande parte da cidade."

    prefix = read_document(
        services, session, principal_id, "zim:wikipedia_pt_teste/Lisboa", section="geo"
    )
    assert prefix.section == "Geografia" and prefix.text.endswith("- Sete colinas")

    cut = read_document(
        services, session, principal_id, "zim:wikipedia_pt_teste/Lisboa", max_chars=100
    )
    assert cut.truncated and len(cut.text) <= 100
    assert cut.total_chars > 100 and cut.text.startswith("Lisboa é a capital")


def test_read_aceita_a_grafia_dos_links_do_kiwix(
    services: Services, session: Session, principal_id: int
) -> None:
    for spelling in ("Capital%20de%20Portugal", "Capital de Portugal", "Capital_de_Portugal"):
        view = read_document(services, session, principal_id, f"zim:wikipedia_pt_teste/{spelling}")
        assert view.document_id == "zim:wikipedia_pt_teste/Lisboa"


def test_read_erros(services: Services, session: Session, principal_id: int) -> None:
    with pytest.raises(UnknownSource, match="Available sources: zim:wikipedia_pt_teste"):
        read_document(services, session, principal_id, "zim:outra/Lisboa")
    with pytest.raises(DocumentNotFound, match="not found"):
        read_document(services, session, principal_id, "zim:wikipedia_pt_teste/Nao_existe")
    with pytest.raises(DocumentNotFound, match="is not a text article"):
        read_document(services, session, principal_id, "zim:wikipedia_pt_teste/_res_/style.css")
    with pytest.raises(DocumentNotFound, match="Invalid document_id"):
        read_document(services, session, principal_id, "sem-barra")
    with pytest.raises(SectionNotFound, match="Available sections: 'Lisboa', 'História'"):
        read_document(
            services, session, principal_id, "zim:wikipedia_pt_teste/Lisboa", section="Economia"
        )


def test_sources(services: Services, session: Session) -> None:
    [info] = list_sources(services, session)
    assert info.size_bytes and info.size_bytes > 0
    assert info.model_dump(exclude={"size_bytes"}) == {
        "id": "zim:wikipedia_pt_teste",
        "title": "Wikipédia (teste)",
        "kind": "zim",
        "language": "pt",
        "version": "2026-09",
        "articles": 4,
        "wing": "wikipedia_pt_teste",
        "rooms": {"count": 0, "top": []},  # sem vetores de artigo ainda não há salas
        "content_class": "article",
        "content_type": "article",
        "archive_only": False,
        "license_class": "open",
    }


# --- integração: kiwix-serve e ZIM reais do compose ---------------------------------------------


@pytest.mark.kiwix
def test_pesquisar_e_ler_no_kiwix_real(
    db: sessionmaker[Session], kiwix_url: str, real_zim: Path, principal_id: int
) -> None:
    services = build_services(db, real_zim.parent, KiwixClient(kiwix_url))
    with db() as session, closing(services):
        result = search(services, session, principal_id, "ray charles blindness", k=5)
        assert result.results, "o kiwix devia achar algo no ZIM de teste"
        assert [h.rank for h in result.results] == list(range(1, len(result.results) + 1))
        hit = result.results[0]
        assert hit.source_id == "zim:wikipedia_en_ray-charles_nopic"
        assert hit.kiwix_url is not None and hit.kiwix_url.startswith("http://")
        # O que o kiwix devolve tem de existir no ZIM lido por libzim.
        view = read_document(services, session, principal_id, hit.document_id)
        assert view.title == hit.title
        assert view.text
        assert session.get(Document, view.document_id) is not None
        none = search(services, session, principal_id, "zzzqqqxxx")
        assert none.results == []
        log = session.get(QueryLog, none.query_id)
        assert log is not None and log.level_served is None and log.top_score is None


def test_caracteres_que_o_postgres_recusa(
    services: Services, session: Session, principal_id: int
) -> None:
    result = search(services, session, principal_id, "capi\x00tal \ud800")
    log = session.get(QueryLog, result.query_id)
    assert log is not None and log.query == "capital ?"
    view = read_document(
        services,
        session,
        principal_id,
        "zim:wikipedia_pt_teste/Lis\x00boa",
        section="Terra\x00moto",
    )
    assert view.document_id == "zim:wikipedia_pt_teste/Lisboa" and view.section == "Terramoto"
    with pytest.raises(DocumentNotFound):
        read_document(services, session, principal_id, "zim:wikipedia_pt_teste/\x00")
