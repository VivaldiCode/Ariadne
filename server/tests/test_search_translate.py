"""Fase 7: a pergunta também é traduzida PT⇄EN e pesquisada nas fontes da outra língua."""

import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, create_principal
from ariadne.concepts.importers import import_sitelinks
from ariadne.db.models import QueryLog, QueryResult
from ariadne.embedder import FakeEmbedder
from ariadne.kiwix import KiwixClient
from ariadne.search import SearchResult, search
from ariadne.services import Services
from ariadne.translate.fake import FakeTranslator
from tests import sitelinks
from tests.fakes import EN_BOOK, PT_BOOK
from tests.langzims import EN_CRAVOS, EN_LISBON, PT_CRAVOS, PT_LISBOA, make_lang_dir
from tests.test_level3 import make_services
from tests.zimfactory import kiwix_books

pytestmark = pytest.mark.db

EN_QUERY = "When was the Carnation Revolution"
EN_TRANSLATED = "Quando foi a Cravo Revolução"  # o FakeTranslator troca palavra a palavra
PT_QUERY = "Quando foi a Revolução dos Cravos"
PT_TRANSLATED = "When was the Revolution of the Carnations"


class PatternKiwix:
    """kiwix-serve de brincar que responde conforme o texto pesquisado e o livro."""

    def __init__(self, results: dict[tuple[str, str], list[tuple[str, str]]]) -> None:
        self.results = results
        self.requests: list[tuple[str, tuple[str, ...]]] = []
        self.delay = 0.0  # segundos que cada pedido demora

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(404)  # sem catálogo: o Ariadne pede todos os livros
        time.sleep(self.delay)
        pattern = request.url.params["pattern"]
        books = tuple(kiwix_books(request.url.params))
        self.requests.append((pattern, books))
        items = "".join(
            f"<item><title>{title}</title><link>/content/{book}/{path}</link>"
            f"<description>excerto de <b>{title}</b></description></item>"
            for book in books
            for path, title in self.results.get((book, pattern), [])
        )
        return httpx.Response(200, text=f"<rss><channel>{items}</channel></rss>")

    def client(self) -> KiwixClient:
        return KiwixClient("http://kiwix", client=httpx.Client(transport=httpx.MockTransport(self)))


def fake_kiwix() -> PatternKiwix:
    return PatternKiwix(
        {
            (EN_BOOK, EN_QUERY): [("Lisbon", "Lisbon")],
            (PT_BOOK, EN_TRANSLATED): [("Revolucao_dos_Cravos", "Revolução dos Cravos")],
            (PT_BOOK, PT_QUERY): [("Lisboa", "Lisboa")],
            (EN_BOOK, PT_TRANSLATED): [("Carnation_Revolution", "Carnation Revolution")],
        }
    )


@pytest.fixture
def kiwix() -> PatternKiwix:
    return fake_kiwix()


@pytest.fixture
def translator() -> FakeTranslator:
    return FakeTranslator()


@pytest.fixture
def services(
    db: sessionmaker[Session], tmp_path: Path, kiwix: PatternKiwix, translator: FakeTranslator
) -> Iterator[Services]:
    # `make_services` pede um FakeKiwix; troca-se o cliente logo a seguir.
    from tests.fakes import default_fake

    services = make_services(db, make_lang_dir(tmp_path / "zim"), default_fake(), FakeEmbedder())
    services.kiwix = kiwix.client()
    services.translator = translator
    yield services
    services.close()


@pytest.fixture
def principal_id(db: sessionmaker[Session]) -> int:
    with db() as session, session.begin():
        return create_principal(session, PrincipalKind.agent, "agente")[0].id


def ask(
    services: Services, db: sessionmaker[Session], principal_id: int, query: str, **kwargs: object
) -> SearchResult:
    with db() as session:
        return search(services, session, principal_id, query, **kwargs)  # type: ignore[arg-type]


def ids(result: SearchResult) -> list[str]:
    return [hit.document_id for hit in result.results]


def test_pergunta_em_ingles_tambem_se_pesquisa_em_portugues(
    services: Services,
    db: sessionmaker[Session],
    principal_id: int,
    kiwix: PatternKiwix,
    translator: FakeTranslator,
) -> None:
    result = ask(services, db, principal_id, EN_QUERY)

    assert set(ids(result)) == {EN_LISBON, PT_CRAVOS}
    by_id = {hit.document_id: hit for hit in result.results}
    assert by_id[PT_CRAVOS].translated_query is True  # só a pergunta traduzida a encontrou
    assert by_id[EN_LISBON].translated_query is False
    assert translator.calls == [(EN_QUERY, "en", "pt")]
    # A original continua a ser pesquisada em todas as línguas; a traduzida só nas da outra.
    patterns = {(pattern, books) for pattern, books in kiwix.requests}
    assert (EN_QUERY, (EN_BOOK,)) in patterns
    assert (EN_TRANSLATED, tuple(sorted((PT_BOOK, "wikibooks_pt_teste_2026-09")))) in {
        (p, tuple(sorted(b))) for p, b in kiwix.requests
    }
    assert not any(books == (EN_BOOK,) and pattern == EN_TRANSLATED for pattern, books in patterns)
    with db() as session:
        flags = {
            r.document_id: r.translated_query
            for r in session.scalars(
                select(QueryResult).where(QueryResult.query_id == result.query_id)
            )
        }
        log = session.get(QueryLog, result.query_id)
    assert flags == {EN_LISBON: False, PT_CRAVOS: True}
    assert log is not None and log.filters is not None
    assert log.filters["translated_query"] == EN_TRANSLATED


def test_pergunta_em_portugues_tambem_se_pesquisa_em_ingles(
    services: Services, db: sessionmaker[Session], principal_id: int, translator: FakeTranslator
) -> None:
    result = ask(services, db, principal_id, PT_QUERY)

    by_id = {hit.document_id: hit for hit in result.results}
    assert set(by_id) == {PT_LISBOA, EN_CRAVOS}
    assert by_id[EN_CRAVOS].translated_query and not by_id[PT_LISBOA].translated_query
    assert translator.calls == [(PT_QUERY, "pt", "en")]


def test_a_traducao_repete_se_vem_da_cache(
    services: Services, db: sessionmaker[Session], principal_id: int, translator: FakeTranslator
) -> None:
    ask(services, db, principal_id, EN_QUERY)
    ask(services, db, principal_id, EN_QUERY)

    assert len(translator.calls) == 1  # a 2.ª pergunta usou a tradução guardada


def test_o_resultado_encontrado_pelas_duas_perguntas_nao_fica_marcado(
    services: Services, db: sessionmaker[Session], principal_id: int, kiwix: PatternKiwix
) -> None:
    kiwix.results[(PT_BOOK, EN_QUERY)] = [("Revolucao_dos_Cravos", "Revolução dos Cravos")]

    result = ask(services, db, principal_id, EN_QUERY)

    assert sorted(ids(result)) == sorted([EN_LISBON, PT_CRAVOS])
    assert not any(hit.translated_query for hit in result.results)


def test_tradutor_em_baixo_a_pesquisa_segue_com_aviso(
    services: Services, db: sessionmaker[Session], principal_id: int, translator: FakeTranslator
) -> None:
    translator.down = True

    result = ask(services, db, principal_id, EN_QUERY)

    assert ids(result) == [EN_LISBON]  # a pesquisa original
    assert any("Translation unavailable" in w for w in result.warnings)
    assert not any(hit.translated_query for hit in result.results)


def test_com_filtro_de_lingua_nao_se_traduz(
    services: Services, db: sessionmaker[Session], principal_id: int, translator: FakeTranslator
) -> None:
    result = ask(services, db, principal_id, EN_QUERY, lang="en")

    assert ids(result) == [EN_LISBON] and translator.calls == []


def test_sem_tradutor_configurado_nada_muda(
    db: sessionmaker[Session], tmp_path: Path, principal_id: int, kiwix: PatternKiwix
) -> None:
    from tests.fakes import default_fake

    services = make_services(db, make_lang_dir(tmp_path / "zim"), default_fake(), FakeEmbedder())
    services.kiwix = kiwix.client()
    try:
        result = ask(services, db, principal_id, EN_QUERY)

        assert ids(result) == [EN_LISBON] and result.warnings == []
        assert not services.get_translation().enabled
    finally:
        services.close()


def test_pergunta_traduzida_desligada_por_configuracao(
    db: sessionmaker[Session], tmp_path: Path, principal_id: int, kiwix: PatternKiwix
) -> None:
    from tests.fakes import default_fake

    services = make_services(
        db, make_lang_dir(tmp_path / "zim"), default_fake(), FakeEmbedder(), translate_queries=False
    )
    fake = FakeTranslator()
    services.kiwix, services.translator = kiwix.client(), fake
    try:
        result = ask(services, db, principal_id, EN_QUERY)

        assert ids(result) == [EN_LISBON] and fake.calls == []
    finally:
        services.close()


def test_a_traducao_e_a_regra_historica_juntas(
    services: Services,
    db: sessionmaker[Session],
    principal_id: int,
    tmp_path: Path,
    kiwix: PatternKiwix,
) -> None:
    from ariadne.concepts import origin

    query = EN_QUERY + " 1974"  # histórica: tem um ano
    kiwix.results[(EN_BOOK, query)] = [("Carnation_Revolution", "Carnation Revolution")]
    kiwix.results[(PT_BOOK, EN_TRANSLATED + " 1974")] = [("Lisboa", "Lisboa")]
    import_sitelinks(
        db, services.registry.active(), sitelinks.write_items_dump(tmp_path / "i.sql.gz")
    )
    with db() as session, session.begin():
        origin.set_origin(session, f"Q{sitelinks.Q_CRAVOS}", "pt")

    result = ask(services, db, principal_id, query)

    flags = {h.document_id: (h.original_source, h.translated_query) for h in result.results}
    # Lisboa só pela tradução; a versão PT da Revolução entra pela regra histórica (não pela busca).
    assert flags == {EN_CRAVOS: (False, False), PT_CRAVOS: (True, False), PT_LISBOA: (False, True)}


class Slow(FakeTranslator):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def translate(
        self, text: str, source: str, target: str, *, timeout: float | None = None
    ) -> str:
        time.sleep(self.delay)
        return super().translate(text, source, target, timeout=timeout)


def test_a_latencia_nao_dobra_a_traducao_corre_em_paralelo_com_a_pesquisa_original(
    services: Services, db: sessionmaker[Session], principal_id: int, kiwix: PatternKiwix
) -> None:
    kiwix.delay = 0.4
    services.translator = Slow(0.4)
    services._translation = None  # type: ignore[attr-defined]

    started = time.perf_counter()
    result = ask(services, db, principal_id, EN_QUERY)
    elapsed = time.perf_counter() - started

    assert PT_CRAVOS in ids(result)
    # Original (0,4) em paralelo com tradução (0,4) + pesquisa traduzida (0,4): ≈ 0,8 s, e não
    # os 1,2 s de fazer tudo em série.
    assert elapsed < 1.05, elapsed


def test_a_pergunta_traduzida_que_rebenta_nao_derruba_a_pesquisa(
    services: Services, db: sessionmaker[Session], principal_id: int, translator: FakeTranslator
) -> None:
    def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("erro inesperado no tradutor")

    translator.translate = explode  # type: ignore[method-assign]

    result = ask(services, db, principal_id, EN_QUERY)

    assert ids(result) == [EN_LISBON]  # a pesquisa original
    assert any("translated question failed" in w for w in result.warnings)


def test_so_se_traduz_com_a_lingua_da_pergunta_decidida(
    services: Services, db: sessionmaker[Session], principal_id: int, kiwix: PatternKiwix
) -> None:
    from ariadne.search import _translation_plan

    plan = _translation_plan(services, EN_QUERY, None, services.registry.active())
    assert plan is not None and plan[:2] == ("en", "pt")
    assert _translation_plan(services, "Lisboa", None, services.registry.active()) is None
