"""Fase 6 contra o Wiki.js 2.5 verdadeiro do compose (`docker compose up -d wikijs`)."""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ariadne.embedder import FakeEmbedder
from ariadne.services import Services
from ariadne.wiki import language
from ariadne.wiki.bootstrap import bootstrap
from ariadne.wiki.wikijs import WikiJsBackend
from tests import conftest
from tests.curationzims import BOOKS_REV, LISBOA, PORTUGAL, make_dir
from tests.fakes import default_fake
from tests.test_curation import link, request_import, run_jobs
from tests.test_level3 import make_services

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]


@pytest.fixture
def backend(wikijs: tuple[str, str]) -> Iterator[WikiJsBackend]:
    url, token = wikijs
    backend = WikiJsBackend(url, token)
    yield backend
    backend.close()


def delete_pages(backend: WikiJsBackend, *page_ids: int) -> None:
    for page_id in page_ids:
        backend._call(
            "mutation($id: Int!) { pages { delete(id: $id) { responseResult { succeeded } } } }",
            {"id": page_id},
        )


def test_arranque_e_idempotente(wikijs: tuple[str, str]) -> None:
    url, _ = wikijs
    env = conftest.TestEnv()

    first = bootstrap(url, env.wikijs_admin_email, env.wikijs_admin_password, url)
    second = bootstrap(url, env.wikijs_admin_email, env.wikijs_admin_password, url)

    assert first != second  # numa instância já configurada cria uma chave nova
    for key in (first, second):
        backend = WikiJsBackend(url, key)
        assert backend.health() == "ok"
        assert backend.search("qualquercoisa", 1) == []
        backend.close()
    assert WikiJsBackend(url, "").health().startswith("ok (no token")


def test_chave_invalida_e_recusada(wikijs: tuple[str, str]) -> None:
    from ariadne.wiki.base import WikiError

    with pytest.raises(WikiError):
        WikiJsBackend(wikijs[0], "chave-falsa").upsert_page("ariadne-teste/x/y", "T", "c", "d", [])


def test_criar_atualizar_ler_e_pesquisar(backend: WikiJsBackend) -> None:
    tag = secrets.token_hex(4)
    path = f"ariadne-teste/{tag}/pagina"
    page_id = backend.upsert_page(path, f"Zebra{tag}", "# Olá\n\ntexto", f"descricao{tag}", ["t"])
    try:
        assert backend.upsert_page(path, f"Zebra{tag}", "# Olá\n\nnovo", "d2", ["t"]) == page_id
        page = backend.get_page(page_id)
        assert page is not None and page.content.endswith("novo") and page.path == path
        hits = backend.search(f"Zebra{tag}", 5)
        assert [h.page_id for h in hits] == [page_id]
        assert backend.search(f"Zebra{tag}", 5, locale="pt") == []
        # Com o namespacing ligado (ambiente, standalone) o Wiki.js redireciona para /<locale>/….
        assert httpx.get(f"{backend.url}/{path}", follow_redirects=True).status_code == 200
    finally:
        delete_pages(backend, page_id)
    assert backend.get_page(page_id) is None


def test_mover_a_pagina_e_locales_ativos(backend: WikiJsBackend) -> None:
    from ariadne.wiki.base import WikiError

    tag = secrets.token_hex(4)
    old, new = f"ariadne-teste/{tag}/antiga", f"ariadne-teste/{tag}/dominio/nova"
    page_id = backend.upsert_page(old, "Mover", "texto", "d", ["en"], locale="en")
    try:
        backend.move_page(page_id, new, "en")
        page = backend.get_page(page_id)
        assert page is not None and page.path == new and page.content == "texto"
        assert backend._find(old, "en") is None
        assert backend.upsert_page(new, "Mover", "outro", "d", ["en"], locale="en") == page_id
        with pytest.raises(WikiError, match=r"já existe|already exists"):
            backend.move_page(page_id, new, "en")  # o destino existe (é ela própria)
        assert "en" in (backend.active_locales() or set())  # o locale do site conta sempre
    finally:
        delete_pages(backend, page_id)


def test_locales_ativos_com_namespacing_e_cache() -> None:
    calls: list[str] = []
    config = {"locale": "en", "namespacing": True, "namespaces": ["en", "pt"]}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        if config["locale"] == "boom":
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"data": {"localization": {"config": config}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = WikiJsBackend("http://wiki", "t", client=client)
    assert backend.active_locales() == frozenset({"en", "pt"})
    config["namespacing"] = False
    assert backend.active_locales() == frozenset({"en", "pt"})  # em cache
    assert len(calls) == 1
    other = WikiJsBackend("http://wiki", "t", client=client)  # sem cache: o namespacing desligado
    assert other.active_locales() == frozenset({"en"})  # só o locale do site
    config["locale"] = "boom"
    assert WikiJsBackend("http://wiki", "t", client=client).active_locales() is None


def test_importacao_real_com_ligacoes_entre_fontes(
    db: sessionmaker[Session],
    tmp_path: Path,
    backend: WikiJsBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O Wiki.js do compose só tem o locale `en` e o *namespacing* desligado (o do ambiente tem
    # `en` e `pt` com o *namespacing* ligado): as páginas portuguesas de teste fazem-se passar por
    # inglesas e o URL fica sem o prefixo do locale (`/unsorted/lisboa`, não `/en/unsorted/lisboa`).
    monkeypatch.setattr(language, "detect", lambda **kw: "en")
    services: Services = make_services(
        db,
        make_dir(tmp_path / "zims"),
        default_fake(),
        FakeEmbedder(),
        wikijs_url=backend.url,
        wikijs_token=backend._headers["Authorization"].split()[1],
        curator_url="",
    )
    page_ids: list[int] = []
    try:
        link(services, LISBOA, BOOKS_REV)
        request_import(services, LISBOA, PORTUGAL, BOOKS_REV)
        run_jobs(services)
        with services.sessions() as session:
            rows = session.execute(
                text(
                    "SELECT id, wiki_page_id, meta->>'wiki_path' FROM document"
                    " WHERE wiki_page_id IS NOT NULL"
                )
            ).all()
        page_ids = [row[1] for row in rows]
        paths = {row[0]: row[2] for row in rows}
        assert paths[LISBOA] == "en/unsorted/lisboa"  # sem vetores: por classificar
        index_id = backend._find("unsorted", "en")  # a página do domínio
        assert index_id is not None
        page_ids.append(index_id)

        html = httpx.get(f"{backend.url}/unsorted/lisboa", follow_redirects=True)  # namespacing
        assert html.status_code == 200
        assert "Ligações" in html.text and "Mesmo título noutras fontes" in html.text
        assert "/en/unsorted/revolucao-dos-cravos" in html.text  # a outra fonte, no Wiki.js
        assert "CC BY-SA 4.0" in html.text and "/en/unsorted/portugal" in html.text
        domain = httpx.get(f"{backend.url}/unsorted", follow_redirects=True)
        assert domain.status_code == 200 and "Lisboa" in domain.text

        before = backend.get_page(rows[0][1])
        request_import(services, LISBOA)
        run_jobs(services)  # idempotente: a página não muda
        assert backend.get_page(rows[0][1]) == before
    finally:
        delete_pages(backend, *page_ids)
        services.close()


def test_arranque_trata_respostas_sem_json() -> None:
    from ariadne.wiki.bootstrap import BootstrapError, _graphql

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad gateway</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(BootstrapError, match="without JSON"):
        _graphql(client, "http://wiki", "query { x }", {}, None)


def test_arranque_endurece_os_renderizadores(
    wikijs: tuple[str, str], backend: WikiJsBackend
) -> None:
    url, _ = wikijs
    env = conftest.TestEnv()
    bootstrap(url, env.wikijs_admin_email, env.wikijs_admin_password, url)  # ... e é idempotente
    data = backend._call("{ rendering { renderers { key isEnabled config { key value } } } }", {})
    config = {
        r["key"]: {c["key"]: json.loads(c["value"])["value"] for c in r["config"]}
        for r in data["rendering"]["renderers"]
    }
    assert config["markdownCore"]["allowHTML"] is False
    assert config["htmlSecurity"]["safeHTML"] is True
