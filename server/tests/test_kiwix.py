from pathlib import Path

import httpx
import pytest

from ariadne.kiwix import (
    KiwixBookMissing,
    KiwixClient,
    KiwixError,
    KiwixUnavailable,
    content_url,
    parse_search,
)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <channel>
    <item>
      <title>Lisboa (cidade)</title>
      <link>/content/wikipedia_pt_teste_2026-09/Lisboa_%28cidade%29</link>
      <description>...a <b>capital</b> de Portugal &amp; do Tejo,
        desde 1255......</description>
      <book><title>Wikipédia</title></book>
    </item>
    <item>
      <title>Fora</title>
      <link>/outra/rota</link>
      <description>ignorado</description>
    </item>
    <item>
      <title>São Tomé</title>
      <link>/content/wikipedia_pt_teste_2026-09/S%C3%A3o_Tom%C3%A9</link>
      <description></description>
    </item>
  </channel>
</rss>"""


def test_parse_search() -> None:
    hits = parse_search(RSS)

    assert [(h.book, h.path, h.title) for h in hits] == [
        ("wikipedia_pt_teste_2026-09", "Lisboa_(cidade)", "Lisboa (cidade)"),
        ("wikipedia_pt_teste_2026-09", "São_Tomé", "São Tomé"),
    ]
    assert hits[0].snippet == "… a capital de Portugal & do Tejo, desde 1255 …"  # sem <b>
    assert hits[1].snippet == ""


def test_parse_search_ilegivel() -> None:
    with pytest.raises(KiwixError):
        parse_search("isto não é xml")


def test_content_url() -> None:
    url = content_url("http://kiwix-host:8801/", "wikipedia_pt_2026-09", "São_Tomé")
    assert url == "http://kiwix-host:8801/content/wikipedia_pt_2026-09/S%C3%A3o_Tom%C3%A9"
    plain = content_url("http://k", "livro", "Ray_(film)")
    assert plain == "http://k/content/livro/Ray_(film)"  # parênteses e apóstrofos ficam legíveis
    assert content_url("http://k", "livro", "A?b#c%d") == "http://k/content/livro/A%3Fb%23c%25d"


def _client(handler: httpx.MockTransport) -> KiwixClient:
    return KiwixClient("http://kiwix", client=httpx.Client(transport=handler))


def test_pedido_leva_os_livros_e_o_tamanho() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, text=RSS)

    hits = _client(httpx.MockTransport(handler)).search("capital", books=["a", "b"], k=1)

    assert len(hits) == 1  # corta em k
    params = seen[0].params
    assert params["pattern"] == "capital"
    assert params["pageLength"] == "1"
    assert params["format"] == "xml"
    assert params.get_list("books.name") == ["a", "b"]


def test_erros_do_kiwix() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusada")

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="<detail>No such book: x_2026-01</detail>")

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(KiwixUnavailable):
        _client(httpx.MockTransport(down)).search("x")
    with pytest.raises(KiwixBookMissing) as excinfo:
        _client(httpx.MockTransport(missing)).search("x", books=["x_2026-01"])
    assert excinfo.value.book == "x_2026-01"
    with pytest.raises(KiwixError, match="500"):
        _client(httpx.MockTransport(broken)).search("x")


# --- integração com o kiwix-serve do compose ----------------------------------------------------


@pytest.mark.kiwix
def test_pesquisa_no_kiwix_do_compose(kiwix_url: str, real_zim: Path) -> None:
    client = KiwixClient(kiwix_url)
    # O kiwix-serve recusa pesquisar livros de línguas diferentes de uma vez: só o livro em inglês.
    ray = next(real_zim.parent.glob("wikipedia_en_ray-charles*.zim")).stem
    hits = client.search("ray charles blindness", books=[ray], k=3)

    assert 1 <= len(hits) <= 3
    assert hits[0].book.startswith("wikipedia_en_ray-charles_nopic_")
    assert hits[0].title and hits[0].path
    assert "<b>" not in hits[0].snippet
    only = client.search("ray", books=[hits[0].book], k=2)
    assert {h.book for h in only} == {hits[0].book}
    assert client.search("zzzqqqxxx", books=[ray]) == []
    with pytest.raises(KiwixBookMissing):
        client.search("ray", books=["livro-que-nao-existe"])
    client.close()


def test_pedido_por_uuid_usa_books_id() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, text=RSS)

    _client(httpx.MockTransport(handler)).search("capital", k=3, book_ids=["u-1", "u-2"])

    assert seen[0].params.get_list("books.id") == ["u-1", "u-2"]
    assert "books.name" not in seen[0].params


def test_catalogo_do_kiwix_em_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            text='<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><id>urn:uuid:aaa</id></entry><entry><id>urn:uuid:bbb</id></entry></feed>",
        )

    client = _client(httpx.MockTransport(handler))
    assert client.known_book_ids() == {"aaa", "bbb"}
    assert client.known_book_ids() == {"aaa", "bbb"}
    assert calls == ["/catalog/v2/entries"]  # o segundo veio da cache


def test_catalogo_ilegivel_da_none() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, text="<rss/>")))
    assert client.known_book_ids() is None
    client = _client(httpx.MockTransport(lambda r: httpx.Response(500)))
    assert client.known_book_ids() is None
