"""Fase 6: importação para o Wiki.js através da curadoria (Wiki.js e curador de brincar)."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ariadne import queue
from ariadne.db.models import Curation, Job, Link
from ariadne.db.models import Document as DocumentRow
from ariadne.documents import DocumentInfo, read_article, split_document_id, upsert_documents
from ariadne.embedder import FakeEmbedder
from ariadne.services import Services
from ariadne.wiki.refresh import enqueue_import
from ariadne.workers.importer import run_import_scan
from ariadne.workers.runner import Runner
from tests.curationzims import (
    ARTICLES,
    BOOKS,
    BOOKS_REV,
    LISBOA,
    MALICIOSO,
    PEDIA,
    PORTUGAL,
    REMEDIO,
    make_dir,
)
from tests.fakes import default_fake
from tests.test_level3 import make_services
from tests.wikifakes import FakeCurator, FakeWiki

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]

PREFIX = "pt/por-classificar"  # locale e domínio: sem vetores, as páginas ficam por classificar


@pytest.fixture
def zims(tmp_path: Path) -> Path:
    return make_dir(tmp_path / "zims")


@pytest.fixture
def curator() -> Iterator[FakeCurator]:
    fake = FakeCurator()
    yield fake
    fake.stop()


def build(db: sessionmaker[Session], zims: Path, **settings: object) -> tuple[Services, FakeWiki]:
    services = make_services(
        db,
        zims,
        default_fake(),
        FakeEmbedder(),
        wikijs_url="http://wiki",
        wikijs_token="t",
        **settings,
    )
    wiki = FakeWiki()
    services.wiki = wiki
    return services, wiki


@pytest.fixture
def env(db: sessionmaker[Session], zims: Path) -> Iterator[tuple[Services, FakeWiki]]:
    """Wiki.js em memória e sem curador configurado."""
    services, wiki = build(db, zims)
    yield services, wiki
    services.close()


@pytest.fixture
def with_curator(
    db: sessionmaker[Session], zims: Path, curator: FakeCurator
) -> Iterator[tuple[Services, FakeWiki]]:
    services, wiki = build(
        db, zims, curator_url=curator.url, curator_model="gemma-falso", batch_auto_publish=True
    )
    yield services, wiki
    services.close()


@pytest.fixture
def with_curator_gated(
    db: sessionmaker[Session], zims: Path, curator: FakeCurator
) -> Iterator[tuple[Services, FakeWiki]]:
    """O predefinido: `BATCH_AUTO_PUBLISH=false`, o que o curador reescreve espera revisão."""
    services, wiki = build(db, zims, curator_url=curator.url, curator_model="gemma-falso")
    yield services, wiki
    services.close()


def curations_asked(curator: FakeCurator) -> list[dict[str, Any]]:
    """Os pedidos de curadoria (não os de `domain_classify`, que o local também faz ao curador)."""
    return [r for r in curator.requests if "<<<ARTICLE-" in r["messages"][-1]["content"]]


def run_jobs(services: Services, workers: tuple[str, ...] = ("importer", "curator")) -> int:
    runner = Runner(services, list(workers))
    done = 0
    while runner.run_once() and done < 50:
        done += 1
    return done


def request_import(services: Services, *document_ids: str, force: bool = False) -> None:
    with services.sessions() as session:
        for document_id in document_ids:
            enqueue_import(session, document_id, 1.0, force=force)
        session.commit()


def curation_of(services: Services, document_id: str) -> Curation:
    with services.sessions() as session:
        row = session.get(Curation, document_id)
        assert row is not None
        session.expunge(row)
        return row


def ensure_docs(services: Services, *document_ids: str) -> None:
    """Regista os documentos (como o `read` o faria), para poderem ter arestas."""
    with services.sessions() as session:
        infos = []
        for document_id in document_ids:
            source_id, path = split_document_id(document_id)
            source = services.registry.get(source_id)
            assert source is not None
            article = read_article(source, path)
            infos.append(DocumentInfo(source, article.path, article.title))
        upsert_documents(session, infos)
        session.commit()


def link(
    services: Services, a: str, b: str, kind: str = "title_match", status: str = "accepted"
) -> None:
    ensure_docs(services, a, b)
    src, dst = sorted((a, b))
    with services.sessions() as session:
        session.add(Link(src_document=src, dst_document=dst, kind=kind, score=1.0, status=status))
        session.commit()


# --- o caminho normal: sem flags publica-se direto, sem LLM ---------------------------------------


def test_artigo_sem_flags_publica_direto_sem_curador(
    env: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, wiki = env

    request_import(services, PORTUGAL)
    run_jobs(services)

    page = wiki.pages[f"{PREFIX}/portugal"]
    assert page.title == "Portugal"
    assert "fundado em 1143" in page.content
    assert curation_of(services, PORTUGAL).decision == "published"
    assert curator.requests == []
    with services.sessions() as session:
        document = session.get(DocumentRow, PORTUGAL)
        assert document is not None
        assert document.wiki_page_id == page.page_id
        assert document.meta["wiki_path"] == f"{PREFIX}/portugal"
        assert document.meta["wiki_version"] == "2026-09"


def test_pagina_tem_proveniencia_licenca_e_links_reescritos(env: tuple[Services, FakeWiki]) -> None:
    services, wiki = env

    request_import(services, LISBOA)
    run_jobs(services)
    content = wiki.pages[f"{PREFIX}/lisboa"].content

    assert content.startswith("> **Fonte:**")
    assert PEDIA in content and "versão 2026-09" in content and "caminho `Lisboa`" in content
    assert "(http://kiwix.publico:8801/content/wikipedia_pt_teste_2026-09/Lisboa)" in content
    assert "limpeza determinística" in content
    # Portugal ainda não está no Wiki.js: o link aponta para o kiwix.
    kiwix = "http://kiwix.publico:8801/content/wikipedia_pt_teste_2026-09/Portugal"
    assert f"[Portugal]({kiwix})" in content
    assert "ariadne:" not in content
    assert "CC BY-SA 4.0" in content and "https://pt.wikipedia.org/wiki/Lisboa" in content
    assert wiki.pages[f"{PREFIX}/lisboa"].description.startswith("Lisboa é a capital")
    assert wiki.tags[f"{PREFIX}/lisboa"] == ["pt", "por-classificar"]  # a língua e o domínio


def test_importar_uma_pagina_refaz_as_vizinhas_e_os_links_passam_para_o_wiki(
    env: tuple[Services, FakeWiki],
) -> None:
    services, wiki = env
    link(services, LISBOA, PORTUGAL, kind="zim_internal")
    request_import(services, LISBOA)
    run_jobs(services)
    assert "kiwix.publico" in wiki.pages[f"{PREFIX}/lisboa"].content

    request_import(services, PORTUGAL)
    run_jobs(services)  # Portugal nasce -> enfileira Lisboa -> os links dela passam para o Wiki.js

    assert f"[Portugal](/{PREFIX}/portugal)" in wiki.pages[f"{PREFIX}/lisboa"].content
    assert f"[Lisboa](/{PREFIX}/lisboa)" in wiki.pages[f"{PREFIX}/portugal"].content


def test_bloco_de_ligacoes_entre_fontes(env: tuple[Services, FakeWiki]) -> None:
    services, wiki = env
    link(services, LISBOA, BOOKS_REV)  # aceite: aparece
    link(services, LISBOA, REMEDIO, kind="vector", status="proposed")  # proposta: não aparece
    link(services, LISBOA, PORTUGAL, kind="zim_internal")  # interna: já está no texto

    request_import(services, LISBOA)
    run_jobs(services)
    content = wiki.pages[f"{PREFIX}/lisboa"].content

    assert "## Ligações" in content and "### Mesmo título noutras fontes" in content
    assert "**Wikilivros (teste) (pt)**" in content
    book_url = "http://kiwix.publico:8801/content/wikibooks_pt_teste_2026-09/Revolucao_dos_Cravos"
    assert f"[Revolução dos Cravos]({book_url})" in content
    assert "Remédio" not in content.split("## Ligações")[1]
    assert "Assuntos semelhantes" not in content

    # A página do outro lado nasce: a ligação passa a apontar para o Wiki.js nas duas.
    request_import(services, BOOKS_REV)
    run_jobs(services)
    assert f"(/{PREFIX}/revolucao-dos-cravos)" in wiki.pages[f"{PREFIX}/lisboa"].content
    assert f"(/{PREFIX}/lisboa)" in wiki.pages[f"{PREFIX}/revolucao-dos-cravos"].content


def test_reimportar_e_idempotente(env: tuple[Services, FakeWiki]) -> None:
    services, wiki = env
    request_import(services, PORTUGAL)
    run_jobs(services)
    assert wiki.writes == [f"{PREFIX}/portugal"]

    request_import(services, PORTUGAL)
    run_jobs(services)
    assert wiki.writes == [f"{PREFIX}/portugal"]  # nada mudou: nada se envia

    request_import(services, PORTUGAL, force=True)
    run_jobs(services)
    assert len(wiki.writes) == 2


def test_aresta_nova_ou_revista_refaz_a_pagina(
    env: tuple[Services, FakeWiki], db: sessionmaker[Session]
) -> None:
    from ariadne.graph import review_link

    services, wiki = env
    request_import(services, LISBOA)
    run_jobs(services)
    assert "## Ligações" not in wiki.pages[f"{PREFIX}/lisboa"].content

    link(services, LISBOA, BOOKS_REV, status="proposed")
    with services.sessions() as session:
        link_id = session.scalars(select(Link.id)).one()
        review_link(session, link_id, "1", "accepted")  # a curadoria aceita -> refresca
        session.commit()
        pending = session.scalars(
            select(Job.kind).where(Job.status == "pending", Job.kind != "domain_classify")
        ).all()  # o pedido ao lote LLM (por-classificar) não conta
    assert pending == ["import_page"]
    run_jobs(services)
    assert "Revolução dos Cravos" in wiki.pages[f"{PREFIX}/lisboa"].content

    with services.sessions() as session:
        review_link(session, link_id, "1", "rejected")
        session.commit()
    run_jobs(services)
    assert "## Ligações" not in wiki.pages[f"{PREFIX}/lisboa"].content


def test_linker_refresca_as_paginas_que_ganham_ligacoes(env: tuple[Services, FakeWiki]) -> None:
    from ariadne.workers.linker import run_linker

    services, wiki = env
    request_import(services, LISBOA)
    run_jobs(services)
    ensure_docs(services, BOOKS_REV, f"{BOOKS}/Receitas")
    with services.sessions() as session:
        session.execute(
            text("UPDATE document SET title = 'Lisboa' WHERE id = :id"), {"id": BOOKS_REV}
        )
        session.commit()

    run_linker(services)
    run_jobs(services)

    assert "### Mesmo título noutras fontes" in wiki.pages[f"{PREFIX}/lisboa"].content


# --- atualizações -----------------------------------------------------------------------------


def test_texto_alterado_volta_a_pipeline_e_gera_revisao(
    env: tuple[Services, FakeWiki], zims: Path, db: sessionmaker[Session]
) -> None:
    services, wiki = env
    request_import(services, PORTUGAL)
    run_jobs(services)
    first = curation_of(services, PORTUGAL).fingerprint

    novos = dict(ARTICLES_NEW)
    (zims / "wikipedia_pt_teste_2026-09.zim").unlink()
    make_dir(zims, novos)
    with services.sessions() as session:
        services.registry.sync(session)
    request_import(services, PORTUGAL)
    run_jobs(services)

    assert curation_of(services, PORTUGAL).fingerprint != first
    assert "fundado em 1143" not in wiki.pages[f"{PREFIX}/portugal"].content
    assert "reino de 1139" in wiki.pages[f"{PREFIX}/portugal"].content
    assert wiki.writes.count(f"{PREFIX}/portugal") == 2  # o Wiki.js guarda a revisão anterior


def test_artigo_desaparecido_fica_arquivado_nao_apagado(
    env: tuple[Services, FakeWiki], zims: Path
) -> None:
    services, wiki = env
    request_import(services, PORTUGAL)
    run_jobs(services)

    (zims / "wikipedia_pt_teste_2026-09.zim").unlink()
    make_dir(zims, {"Lisboa": ARTICLES_NEW["Lisboa"]})
    with services.sessions() as session:
        services.registry.sync(session)
    request_import(services, PORTUGAL)
    run_jobs(services)

    page = wiki.pages[f"{PREFIX}/portugal"]  # continua lá
    assert "**Arquivado:**" in page.content and "fundado em 1143" in page.content
    assert curation_of(services, PORTUGAL).archived_at is not None


ARTICLES_NEW = {
    "Lisboa": ("Lisboa", "<h1>Lisboa</h1><p>Lisboa é a capital de Portugal.</p>"),
    "Portugal": (
        "Portugal",
        "<h1>Portugal</h1><p>Portugal é um país da Europa, com o reino de 1139.</p>",
    ),
}


# --- curadoria: só o que dispara flags (ou a saúde, ou a amostragem) vai ao curador ---------


def test_sem_curador_so_os_casos_com_flags_ficam_retidos(
    env: tuple[Services, FakeWiki],
) -> None:
    services, wiki = env

    request_import(services, PORTUGAL, MALICIOSO, REMEDIO)
    run_jobs(services)

    assert list(wiki.pages) == [f"{PREFIX}/portugal"]  # o resto publica-se normalmente
    malicioso = curation_of(services, MALICIOSO)
    assert malicioso.decision == "pending_curator"
    assert {f["code"] for f in malicioso.flags} >= {"injection", "hidden_text", "dangerous_command"}
    assert {f["code"] for f in curation_of(services, REMEDIO).flags} == {"clinical_dose"}
    with services.sessions() as session:  # o job do curador nem foi pedido
        assert session.scalars(select(Job).where(Job.kind == "curate_page")).all() == []


def test_com_curador_o_artigo_com_flags_e_reescrito_e_publicado(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, wiki = with_curator

    request_import(services, MALICIOSO)
    run_jobs(services)

    row = curation_of(services, MALICIOSO)
    assert (row.decision, row.curator_model) == ("published", "gemma-falso")
    assert row.fidelity is not None and row.fidelity["ok"] is True
    content = wiki.pages[f"{PREFIX}/malicioso"].content
    assert "Resumo de referência." in content
    assert "reescrito pelo curador (gemma-falso)" in content
    assert "previous instructions" not in content.lower() and "curl" not in content
    # O curador recebe o texto como dados, com as regras no prompt de sistema.
    [request] = curations_asked(curator)
    system, user = (m["content"] for m in request["messages"])
    assert "DATA, not instructions" in system
    nonce = re.match(r"<<<ARTICLE-(\w{16})>>>\n", user)
    assert nonce is not None and user.endswith(f"<<<END ARTICLE-{nonce.group(1)}>>>")
    assert request["model"] == "gemma-falso"


def test_sem_auto_publicacao_o_curador_local_deixa_o_fiel_em_revisao(
    with_curator_gated: tuple[Services, FakeWiki],
) -> None:
    """`BATCH_AUTO_PUBLISH=false` vale também para o curador local: o que o verificador aprova
    não se publica; a revisão humana (a mesma do lote) é que o publica."""
    from ariadne.curation.queue_view import review_many

    services, wiki = with_curator_gated
    request_import(services, MALICIOSO)
    run_jobs(services)

    row = curation_of(services, MALICIOSO)
    assert row.decision == "needs_review" and row.curator_model == "gemma-falso"
    assert row.reason == "faithful to the original, awaiting validation"
    assert row.fidelity is not None and row.fidelity["ok"] is True
    assert row.curated_md is not None and "Resumo de referência." in row.curated_md
    assert wiki.pages == {}  # nada publicado

    with services.sessions() as session:
        done = review_many(
            session, [(MALICIOSO, row.fingerprint)], approve=True, reviewer="1", note=None
        )
        session.commit()
    assert done.done == [MALICIOSO]
    run_jobs(services)
    assert curation_of(services, MALICIOSO).decision == "published"
    content = wiki.pages[f"{PREFIX}/malicioso"].content
    assert "aprovado por revisão humana" in content and "Resumo de referência." in content


def test_com_auto_publicacao_o_curador_local_publica_o_fiel(
    with_curator: tuple[Services, FakeWiki],
) -> None:
    services, wiki = with_curator
    request_import(services, MALICIOSO)
    run_jobs(services)

    row = curation_of(services, MALICIOSO)
    assert row.decision == "published" and row.reason == "curated and faithful to the original"
    assert f"{PREFIX}/malicioso" in wiki.pages


def test_curador_que_altera_um_numero_nao_passa_e_vai_para_revisao(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, wiki = with_curator
    curator.mode = "alter"

    request_import(services, REMEDIO)
    run_jobs(services)

    row = curation_of(services, REMEDIO)
    assert row.decision == "needs_review"
    assert row.fidelity is not None and row.fidelity["ok"] is False
    assert "quantities added" in (row.reason or "")
    assert row.curated_md is not None and "5000" in row.curated_md
    assert wiki.pages == {}  # nada publicado


@pytest.mark.parametrize("mode", ["reject", "invalid"])
def test_curador_que_rejeita_ou_responde_mal_vai_para_revisao(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator, mode: str
) -> None:
    services, wiki = with_curator
    curator.mode = mode

    request_import(services, MALICIOSO)
    run_jobs(services)

    assert curation_of(services, MALICIOSO).decision == "needs_review"
    assert wiki.pages == {}


def test_curador_em_baixo_adia_sem_perder_o_item(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, wiki = with_curator
    curator.mode = "down"

    request_import(services, MALICIOSO)
    run_jobs(services)

    assert curation_of(services, MALICIOSO).decision == "pending_curator"
    with services.sessions() as session:
        job = session.scalars(select(Job).where(Job.kind == "curate_page")).one()
        assert job.status == "pending" and job.attempts == 0 and "503" in (job.last_error or "")
    assert wiki.pages == {}


def test_saude_e_sempre_curada_e_a_amostragem_tambem(
    db: sessionmaker[Session], zims: Path, curator: FakeCurator
) -> None:
    services, _wiki = build(
        db,
        zims,
        curator_url=curator.url,
        curator_model="m",
        curation_health_sources="wikibooks_pt",
        curation_sample_rate=1.0,
    )
    try:
        request_import(services, BOOKS_REV, PORTUGAL)
        run_jobs(services)

        books = curation_of(services, BOOKS_REV)
        assert {f["code"] for f in books.flags} == {"health"} and books.curator_model == "m"
        portugal = curation_of(services, PORTUGAL)  # sem flags mas amostrado
        assert portugal.flags == [] and portugal.sampled and portugal.curator_model == "m"
        assert len(curations_asked(curator)) == 2
    finally:
        services.close()


def test_sem_amostragem_por_omissao(env: tuple[Services, FakeWiki], curator: FakeCurator) -> None:
    services, _ = env
    assert services.settings.curation_sample_rate == 0.0


def test_texto_novo_cancela_a_curadoria_do_antigo(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    from ariadne.curation import pipeline

    services, _ = with_curator
    request_import(services, MALICIOSO)
    with services.sessions() as session:  # importa mas o curador ainda não correu
        Runner(services, ["importer"]).run_once()
        old = curation_of(services, MALICIOSO)
        assert old.decision == "pending_curator"
        saved = pipeline.finish_curation(
            session,
            MALICIOSO,
            "outra-impressao-digital",
            decision="published",
            reason="x",
            model="m",
            curated_md="x",
            fidelity=None,
        )
        assert saved is False  # o texto mudou entretanto: o resultado antigo não vale


# --- a saída do curador não é de confiança ------------------------------------------------------


def test_reescrita_que_ainda_tem_injecao_nao_se_publica(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, wiki = with_curator
    curator.mode = "evil"  # devolve o texto com a injeção, mais HTML e um link de fora

    request_import(services, MALICIOSO)
    run_jobs(services)

    row = curation_of(services, MALICIOSO)
    assert row.decision == "needs_review"
    assert "the rewrite still has" in (row.reason or "") and "injection" in (row.reason or "")
    assert row.fidelity is not None and "injection" in row.fidelity["remaining_flags"]
    assert row.curated_md is not None and "evil.example" not in row.curated_md
    assert "<img" not in row.curated_md
    assert wiki.pages == {}


def test_html_links_e_urls_de_fora_nao_passam(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, wiki = with_curator
    curator.mode = "tricks"

    request_import(services, MALICIOSO)
    run_jobs(services)

    assert curation_of(services, MALICIOSO).decision == "published"
    content = wiki.pages[f"{PREFIX}/malicioso"].content
    assert "evil.example" not in content and "javascript:" not in content
    assert (
        "<img" not in content
        and "<script" not in content
        and "onerror" not in content.replace("&lt;img src=x onerror", "")
    )
    assert "[1]:" not in content and "clica" in content  # o texto do link fica, o link não


@pytest.mark.parametrize("mode", ["short", "empty"])
def test_reescrita_curta_ou_vazia_nunca_publica_nem_cai_para_o_original(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator, mode: str
) -> None:
    services, wiki = with_curator
    curator.mode = mode

    request_import(services, MALICIOSO)
    run_jobs(services)

    row = curation_of(services, MALICIOSO)
    assert row.decision == "needs_review" and "too short" in (row.reason or "")
    assert wiki.pages == {}
    # nem à força: um `published` sem curado e com flags não publica o texto original
    with services.sessions() as session:
        session.execute(
            text(
                "UPDATE curation SET decision = 'published', curated_md = NULL"
                " WHERE document_id = :i"
            ),
            {"i": MALICIOSO},
        )
        session.commit()
    request_import(services, MALICIOSO, force=True)
    run_jobs(services, ("importer",))
    assert wiki.pages == {}


def test_curador_lento_renova_o_bloqueio_do_job(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ariadne.workers import curator as curator_worker

    services, _ = with_curator
    beats: list[int] = []
    real_touch = queue.touch
    monkeypatch.setattr(curator_worker, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(
        curator_worker.queue,
        "touch",
        lambda session, job_id, worker_id: (
            beats.append(job_id) or real_touch(session, job_id, worker_id)
        ),
    )
    curator.delay = 0.4
    request_import(services, MALICIOSO)

    run_jobs(services)

    assert len(beats) >= 3
    assert curation_of(services, MALICIOSO).decision == "published"


def test_prompt_neutraliza_o_delimitador_com_nonce(
    with_curator: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, _ = with_curator
    request_import(services, MALICIOSO)
    run_jobs(services)

    user = curator.requests[0]["messages"][-1]["content"]
    assert user.count("<<<END ARTICLE-") == 1  # o conteúdo não tem como fechar o bloco
    assert re.search(r"<<<ARTICLE-[0-9a-f]{16}>>>\nTitle: Malicioso\n", user)


# --- itens retidos não ficam presos --------------------------------------------------------------


def test_pending_curator_volta_a_fila_quando_o_curador_aparece(
    env: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    from ariadne.curation.curator import Curator

    services, wiki = env
    services.settings.batch_auto_publish = True
    request_import(services, MALICIOSO)
    run_jobs(services)
    assert curation_of(services, MALICIOSO).decision == "pending_curator"
    assert run_import_scan(services).curation == 0  # ainda sem curador: nada a fazer

    services.curator = Curator(curator.url, "gemma-falso")  # o CURATOR_URL passou a existir
    assert run_import_scan(services).curation == 1
    assert run_import_scan(services).curation == 0  # já tem job pendente
    run_jobs(services)

    assert curation_of(services, MALICIOSO).decision == "published"
    assert f"{PREFIX}/malicioso" in wiki.pages


def test_pending_curator_com_job_falhado_tenta_de_novo_depois_do_recuo(
    with_curator: tuple[Services, FakeWiki],
) -> None:
    services, _ = with_curator
    request_import(services, MALICIOSO)
    Runner(services, ["importer"]).run_once()
    with services.sessions() as session:
        session.execute(
            text("UPDATE job SET status = 'failed', finished_at = now() WHERE kind = 'curate_page'")
        )
        session.commit()
    assert run_import_scan(services).curation == 0  # falhou há pouco: espera

    with services.sessions() as session:
        session.execute(
            text(
                "UPDATE job SET finished_at = now() - interval '2 days' WHERE kind = 'curate_page'"
            )
        )
        session.commit()
    assert run_import_scan(services).curation == 1


# --- atualizações e caminhos ----------------------------------------------------------------------


def test_artigo_que_reaparece_com_o_mesmo_texto_sai_do_arquivo(
    env: tuple[Services, FakeWiki], zims: Path
) -> None:
    services, wiki = env
    request_import(services, PORTUGAL)
    run_jobs(services)
    original = (zims / "wikipedia_pt_teste_2026-09.zim").read_bytes()
    (zims / "wikipedia_pt_teste_2026-09.zim").unlink()
    make_dir(zims, {"Lisboa": ARTICLES_NEW["Lisboa"]})
    with services.sessions() as session:
        services.registry.sync(session)
    request_import(services, PORTUGAL)
    run_jobs(services)
    assert "**Arquivado:**" in wiki.pages[f"{PREFIX}/portugal"].content

    (zims / "wikipedia_pt_teste_2026-09.zim").write_bytes(original)
    with services.sessions() as session:
        services.registry.sync(session)
    request_import(services, PORTUGAL)
    run_jobs(services)

    assert "**Arquivado:**" not in wiki.pages[f"{PREFIX}/portugal"].content
    assert curation_of(services, PORTUGAL).archived_at is None


def test_versao_nova_da_fonte_com_o_mesmo_texto_nao_reescreve_a_pagina(
    env: tuple[Services, FakeWiki], zims: Path
) -> None:
    from tests.zimfactory import make_zim

    services, wiki = env
    request_import(services, PORTUGAL)
    run_jobs(services)
    assert len(wiki.writes) == 1

    (zims / "wikipedia_pt_teste_2026-09.zim").unlink()
    make_zim(
        zims / "wikipedia_pt_teste_2026-09.zim", articles=ARTICLES, redirects={}, date="2026-10-01"
    )
    with services.sessions() as session:
        services.registry.sync(session)
    request_import(services, PORTUGAL)
    run_jobs(services)

    assert len(wiki.writes) == 1  # o texto é igual (e o livro do kiwix o mesmo): nada se envia
    with services.sessions() as session:
        document = session.get(DocumentRow, PORTUGAL)
        assert document is not None and document.meta["wiki_version"] == "2026-10"


def test_dois_titulos_com_o_mesmo_slug_nao_pisam_a_mesma_pagina(
    db: sessionmaker[Session], tmp_path: Path
) -> None:
    articles = {
        "Foo_Bar": ("Foo Bar", "<h1>Foo Bar</h1><p>Primeiro artigo, sobre um assunto.</p>"),
        "Foo-Bar": ("Foo-Bar", "<h1>Foo-Bar</h1><p>Segundo artigo, sobre outro assunto.</p>"),
    }
    services, wiki = build(db, make_dir(tmp_path / "z", articles))
    try:
        request_import(services, f"{PEDIA}/Foo_Bar", f"{PEDIA}/Foo-Bar")
        run_jobs(services)

        assert len(wiki.pages) == 2
        contents = sorted(p.content for p in wiki.pages.values())
        assert (
            "Primeiro artigo" in contents[0] + contents[1]
            and "Segundo artigo" in contents[1] + contents[0]
        )
        assert f"{PREFIX}/foo-bar" in wiki.pages
        with services.sessions() as session:
            paths = session.scalars(
                text("SELECT meta->>'wiki_path' FROM document WHERE wiki_page_id IS NOT NULL")
            ).all()
        assert len(set(paths)) == 2
    finally:
        services.close()


def test_o_indice_recusa_o_mesmo_caminho_em_dois_documentos(env: tuple[Services, FakeWiki]) -> None:
    from sqlalchemy.exc import IntegrityError

    services, _ = env
    ensure_docs(services, LISBOA, PORTUGAL)
    patch = text('UPDATE document SET meta = meta || \'{"wiki_path": "x/y/z"}\' WHERE id = :i')
    with services.sessions() as session:
        session.execute(patch, {"i": LISBOA})
        session.commit()
        with pytest.raises(IntegrityError):
            session.execute(patch, {"i": PORTUGAL})
