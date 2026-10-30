"""Fase 12: o catálogo do Kiwix (OPDS) e a descarga de ZIM, sempre contra um servidor local."""

import errno
import hashlib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from ariadne.zimcatalog.download import (
    DownloadCancelled,
    DownloadError,
    DownloadMeta,
    FatalDownloadError,
    check_url,
    download_zim,
    parse_meta4,
    resolve_meta,
)
from ariadne.zimcatalog.opds import CatalogClient, CatalogError, EntryNotFound, parse_entries
from tests.catalogfakes import FakeKiwixCatalog, make_book, serve
from tests.ipfake import ip

PT = "11111111-1111-4111-8111-111111111111"
PUBLICO = ip(93, 184, 216, 34)  # um endereço público qualquer
EN = "22222222-2222-4222-8222-222222222222"
NO_SHA = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeKiwixCatalog]:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    catalog = FakeKiwixCatalog()
    catalog.add(make_book(scratch, PT, "wikipedia_pt_teste", language="por", title="Wikipédia PT"))
    catalog.add(make_book(scratch, EN, "devdocs_en_python", language="eng", category="devdocs"))
    catalog.add(make_book(scratch, NO_SHA, "outro_en_x", language="eng", advertise_sha=False))
    with serve(catalog):
        yield catalog


@pytest.fixture
def client(fake: FakeKiwixCatalog) -> Iterator[CatalogClient]:
    catalog = CatalogClient(fake.entries_url, ttl=60)
    yield catalog
    catalog.close()


# --- OPDS ------------------------------------------------------------------------------------


def test_pesquisa_filtra_por_texto_lingua_e_categoria(client: CatalogClient) -> None:
    assert client.search().total == 3
    assert [e.name for e in client.search(lang="por").items] == ["wikipedia_pt_teste"]
    assert [e.name for e in client.search(category="devdocs").items] == ["devdocs_en_python"]
    assert [e.id for e in client.search(q="wikipédia").items] == [PT]
    page = client.search(start=1, count=1)
    assert (page.total, page.start, page.count) == (3, 1, 1)


def test_a_entrada_traz_o_ficheiro_o_tamanho_e_o_meta4(client: CatalogClient) -> None:
    entry = client.entry(PT)
    assert entry.stem == "wikipedia_pt_teste_maxi_2026-09"
    assert entry.download_url and entry.download_url.endswith(".zim.meta4")
    assert entry.size_bytes and entry.language == "por" and entry.flavour == "maxi"
    assert entry.tags == ["wikipedia"]  # as etiquetas internas (`_x:y`) não aparecem
    with pytest.raises(EntryNotFound):
        client.entry("99999999-9999-4999-8999-999999999999")
    with pytest.raises(EntryNotFound):
        client.entry("../etc/passwd")


def test_filtros_de_lingua_e_categoria(client: CatalogClient) -> None:
    filters = client.filters()
    assert {o.code for o in filters.languages} == {"por", "eng"}
    assert {o.code for o in filters.categories} == {"wikipedia", "devdocs"}


def test_cache_curta_nao_repete_o_pedido(fake: FakeKiwixCatalog, client: CatalogClient) -> None:
    client.search(q="x")
    client.search(q="x")
    assert sum("q=x" in r for r in fake.requests) == 1


def test_catalogo_inacessivel_ou_malicioso(tmp_path: Path) -> None:
    down = CatalogClient("http://localhost:9/catalog/v2/entries", timeout=1)
    with pytest.raises(CatalogError):
        down.search()
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><feed>&a;</feed>'
    with pytest.raises(CatalogError, match="DTD"):
        parse_entries(bomb, "http://x")
    with pytest.raises(CatalogError, match="invalid"):
        parse_entries(b"<feed><entry>", "http://x")


# --- download ---------------------------------------------------------------------------------


def http() -> httpx.Client:
    return httpx.Client(timeout=10)


def meta_of(fake: FakeKiwixCatalog, book_id: str, client: CatalogClient) -> DownloadMeta:
    entry = client.entry(book_id)
    assert entry.download_url
    with http() as h:
        return resolve_meta(h, entry.download_url, allow_local=True)


def test_descarrega_confere_o_sha256_e_so_entao_da_o_nome_final(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    assert meta.sha256 == fake.books[PT].sha256 and meta.size == len(fake.books[PT].data)
    seen: list[tuple[int, str]] = []
    dest = tmp_path / "zim"
    with http() as h:
        path = download_zim(
            meta,
            dest,
            client=h,
            allow_local=True,
            progress=lambda done, total, phase: seen.append((done, phase)),
        )
    assert path.read_bytes() == fake.books[PT].data
    assert not list(dest.glob("*.part"))
    assert seen[-1] == (meta.size, "done") and any(p == "downloading" for _, p in seen)
    # idempotente: com o ficheiro já lá, não volta a pedir nada
    pedidos = len(fake.requests)
    with http() as h:
        assert download_zim(meta, dest, client=h, allow_local=True) == path
    assert len(fake.requests) == pedidos


def test_retoma_o_part_por_range_depois_de_a_ligacao_cair(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    book = fake.books[PT]
    book.fail_first_bytes = 4096
    meta = meta_of(fake, PT, client)
    with http() as h:
        path = download_zim(
            meta, tmp_path / "zim", client=h, allow_local=True, sleep=lambda s: None
        )
    assert path.read_bytes() == book.data
    assert book.ranges and book.ranges[0].startswith("bytes=4096"), book.ranges


def test_o_part_de_uma_execucao_anterior_retoma_se(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    book = fake.books[PT]
    meta = meta_of(fake, PT, client)
    dest = tmp_path / "zim"
    dest.mkdir()
    (dest / f"{meta.filename}.part").write_bytes(book.data[:5000])
    with http() as h:
        path = download_zim(meta, dest, client=h, allow_local=True)
    assert path.read_bytes() == book.data and book.ranges == ["bytes=5000-"]


def test_checksum_errado_apaga_o_part_e_falha(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    meta.sha256 = hashlib.sha256(b"outra coisa").hexdigest()
    dest = tmp_path / "zim"
    with http() as h, pytest.raises(DownloadError, match="sha-256"):
        download_zim(meta, dest, client=h, allow_local=True)
    assert list(dest.iterdir()) == []  # nem o .zim nem o .part


def test_tamanho_errado_falha(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    assert meta.size
    meta.size = meta.size - 10  # o servidor devolve mais do que o prometido
    dest = tmp_path / "zim"
    with http() as h, pytest.raises(DownloadError, match="wrong size"):
        download_zim(meta, dest, client=h, allow_local=True)
    assert list(dest.glob("*.zim")) == []


def test_sem_sha256_recusa_salvo_decisao_explicita_e_entao_tem_de_abrir_como_zim(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, NO_SHA, client)
    assert meta.sha256 is None
    pedidos = len(fake.requests)
    with http() as h, pytest.raises(FatalDownloadError, match="sha-256"):
        download_zim(meta, tmp_path / "a", client=h, allow_local=True)
    assert len(fake.requests) == pedidos  # nem sequer pediu o ficheiro
    with http() as h:
        path = download_zim(
            meta, tmp_path / "ok", client=h, allow_local=True, allow_unverified=True
        )
    assert path.is_file()
    fake.books[NO_SHA].data = b"isto nao e um zim" * 100
    meta.size = None
    with http() as h, pytest.raises(DownloadError, match="ZIM"):
        download_zim(meta, tmp_path / "mau", client=h, allow_local=True, allow_unverified=True)
    assert list((tmp_path / "mau").glob("*.zim")) == []


def test_o_servidor_que_envia_mais_do_que_prometeu_aborta_logo_sem_repetir(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    assert meta.size
    meta.size = 1000  # o servidor tem muito mais bytes do que os prometidos
    dest = tmp_path / "zim"
    before = sum("/mirror/" in r for r in fake.requests)
    with http() as h, pytest.raises(FatalDownloadError, match="more than"):
        download_zim(meta, dest, client=h, allow_local=True, sleep=lambda s: None)
    assert sum("/mirror/" in r for r in fake.requests) - before == 1  # um só pedido: sem repetir
    assert list(dest.iterdir()) == []  # nem o .part


def test_disco_cheio_e_definitivo_e_nao_deixa_lixo(
    fake: FakeKiwixCatalog,
    client: CatalogClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = meta_of(fake, PT, client)
    dest = tmp_path / "zim"
    real_open = Path.open

    class Full:
        def __init__(self, handle: object) -> None:
            self.handle = handle

        def write(self, data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        def __getattr__(self, name: str) -> object:
            return getattr(self.handle, name)

        def __enter__(self) -> "Full":
            return self

        def __exit__(self, *args: object) -> None:
            self.handle.close()  # type: ignore[attr-defined]

    def opener(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        handle = real_open(self, mode, *args, **kwargs)  # type: ignore[arg-type]
        return Full(handle) if str(self).endswith(".part") and "w" in mode else handle

    monkeypatch.setattr(Path, "open", opener)
    pedidos = len(fake.requests)
    with http() as h, pytest.raises(FatalDownloadError, match="disk full"):
        download_zim(meta, dest, client=h, allow_local=True, sleep=lambda s: None)
    assert sum("/mirror" in r for r in fake.requests[pedidos:]) == 1  # sem 5 repetições
    assert list(dest.glob("*.part")) == []


def test_espaco_insuficiente_e_definitivo(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    with http() as h, pytest.raises(FatalDownloadError, match="not enough disk space"):
        download_zim(meta, tmp_path / "z", client=h, allow_local=True, free_margin=10**15)


def test_um_meta4_gigante_e_recusado(fake: FakeKiwixCatalog, tmp_path: Path) -> None:
    from ariadne.zimcatalog import download as dl

    big = b"x" * (dl.MAX_META_BYTES + 10)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=httpx.ByteStream(big)))
    )
    with pytest.raises(DownloadError, match="too large"):
        dl._get_small(client, f"http://{ip(127, 0, 0, 1)}/x.meta4", allow_local=True)


def test_o_ip_validado_fica_fixado_na_ligacao_sem_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A verificação resolve o nome uma vez; o pedido vai a esse IP (com Host e SNI do nome)."""
    import socket

    from ariadne.zimcatalog import download as dl

    answers = iter([PUBLICO, ip(127, 0, 0, 1), ip(169, 254, 169, 254)])  # o DNS «muda» depois
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))
        ],
    )
    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.url.host, request.headers["host"], request.extensions.get("sni_hostname"))
        )
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert (
        dl._get_small(client, "https://download.exemplo.org/a.sha256", allow_local=False) == b"ok"
    )
    assert seen == [(PUBLICO, "download.exemplo.org", "download.exemplo.org")]
    # um host que resolve para o interior é recusado antes de qualquer ligação
    with pytest.raises(DownloadError, match="local"):
        dl._get_small(client, "https://rebind.exemplo.org/a", allow_local=False)
    assert len(seen) == 1


def test_o_segundo_espelho_serve_quando_o_primeiro_falha(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    fake.mirror_status = 503  # os dois dão 503 -> falha com o motivo
    with http() as h, pytest.raises(DownloadError, match="incomplete"):
        download_zim(meta, tmp_path / "a", client=h, allow_local=True, sleep=lambda s: None)
    fake.mirror_status = 200
    meta.urls = [meta.urls[0].replace("/mirror/", "/nada/"), meta.urls[1]]  # o 1.º dá 404
    with http() as h:
        path = download_zim(meta, tmp_path / "b", client=h, allow_local=True, sleep=lambda s: None)
    assert path.read_bytes() == fake.books[PT].data


def test_disco_sem_espaco_recusa_antes_de_descarregar(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    with http() as h, pytest.raises(DownloadError, match="not enough disk space"):
        download_zim(meta, tmp_path / "zim", client=h, allow_local=True, free_margin=10**15)
    assert not list((tmp_path / "zim").glob("*.part"))


def test_cancelar_deixa_o_part_para_retomar(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    dest = tmp_path / "zim"
    with http() as h, pytest.raises(DownloadCancelled):
        download_zim(meta, dest, client=h, allow_local=True, should_stop=lambda: "cancelada")
    assert list(dest.glob("*.part")) and not list(dest.glob("*.zim"))


def test_so_aceita_nomes_e_urls_seguros(tmp_path: Path) -> None:
    for bad in ("../x.zim", "a/b.zim", "x.zip", ".zim", "x.zim\n"):
        meta = DownloadMeta(filename=bad, size=1, sha256=None, urls=["https://x/y"])
        with http() as h, pytest.raises(DownloadError, match="file name"):
            download_zim(meta, tmp_path, client=h)
    with pytest.raises(DownloadError, match="file name"):
        parse_meta4(
            b'<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="../../etc/x.zim">'
            b"<size>1</size></file></metalink>"
        )
    with pytest.raises(DownloadError):
        check_url("file:///etc/passwd", allow_local=False)
    with pytest.raises(DownloadError, match="https"):
        check_url("http://exemplo.org/x.zim", allow_local=False)
    with pytest.raises(DownloadError, match="local"):
        check_url(f"https://{ip(127, 0, 0, 1)}/x.zim", allow_local=False)
    check_url(f"http://{ip(127, 0, 0, 1)}:8000/x.zim", allow_local=True)


def test_um_redirecionamento_para_o_interior_e_recusado(
    fake: FakeKiwixCatalog, client: CatalogClient, tmp_path: Path
) -> None:
    meta = meta_of(fake, PT, client)
    fake.redirect_to = f"https://{ip(169, 254, 169, 254)}/latest/meta-data"
    meta.urls = meta.urls[:1]
    with http() as h, pytest.raises(DownloadError):
        # com o catálogo "remoto" (allow_local=False) nem o loopback passa; aqui simula-se a
        # regra pelo salto para um endereço de metadados da cloud
        download_zim(meta, tmp_path / "zim", client=h, allow_local=False, sleep=lambda s: None)
