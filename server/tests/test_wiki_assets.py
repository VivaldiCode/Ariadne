"""Envio das imagens para os assets do Wiki.js (WIKI-IMPORT.md, secção 3).

O `ImageUploader` corre contra um Wiki.js em memória e uma fonte de brincar. O último teste envia
uma imagem a sério para o Wiki.js do compose (salta-se se ele não responder).
"""

import hashlib
import logging
import secrets
import struct
import zlib
from collections.abc import Iterator

import httpx
import pytest

from ariadne.sources.base import NotFound
from ariadne.wiki import assets, convert
from ariadne.wiki.assets import ImageUploader, image_size, sniff_mimetype
from ariadne.wiki.base import WikiError, WikiUnavailable
from ariadne.wiki.boilerplate import Boilerplate
from ariadne.wiki.convert import ImageRef
from ariadne.wiki.wikijs import WikiJsBackend
from tests import conftest
from tests.wikifakes import FakeWiki

# --- imagens mínimas, com cabeçalhos verdadeiros ---------------------------------------------


def png(width: int = 64, height: int = 64, seed: int = 0) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + bytes([seed % 256, 50, 200]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", height, width) + b"\x03\x01\x22\x00" * 3
    return b"\xff\xd8" + app0 + sof + b"\x00" * 16 + b"\xff\xd9"


def webp(kind: str, width: int, height: int) -> bytes:
    if kind == "VP8X":
        chunk = (
            b"VP8X\x0a\x00\x00\x00\x00\x00\x00\x00"
            + (width - 1).to_bytes(3, "little")
            + (height - 1).to_bytes(3, "little")
        )
    elif kind == "VP8L":
        chunk = b"VP8L\x05\x00\x00\x00\x2f" + struct.pack("<I", (width - 1) | ((height - 1) << 14))
    else:  # VP8 (com perdas): 3 bytes de quadro, o código de início e depois as dimensões
        chunk = b"VP8 \x0a\x00\x00\x00\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk + b"\x00" * 8


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect/></svg>'


class FakeSource:
    """Lê «entradas em bruto» de um dicionário, como `ZimSource.read_bytes`."""

    def __init__(self, files: dict[str, tuple[str, bytes]]) -> None:
        self.files = files
        self.reads: list[str] = []

    def read_bytes(self, path: str) -> tuple[str, bytes]:
        self.reads.append(path)
        try:
            return self.files[path]
        except KeyError:
            raise NotFound(path) from None


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- cabeçalhos ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "size", "mimetype"),
    [
        (png(70, 40), (70, 40), "image/png"),
        (gif(31, 600), (31, 600), "image/gif"),
        (jpeg(800, 33), (800, 33), "image/jpeg"),
        (webp("VP8X", 123, 45), (123, 45), "image/webp"),
        (webp("VP8L", 640, 480), (640, 480), "image/webp"),
        (webp("VP8", 100, 50), (100, 50), "image/webp"),
        (SVG, None, "image/svg+xml"),
        (b"<?xml version='1.0'?>\n" + SVG, None, "image/svg+xml"),
        (b"nada disto e uma imagem", None, ""),
        (b"\x89PNG\r\n\x1a\ntruncado", None, "image/png"),
    ],
)
def test_tamanho_e_tipo_lidos_do_cabecalho(
    data: bytes, size: tuple[int, int] | None, mimetype: str
) -> None:
    assert image_size(data) == size
    assert sniff_mimetype(data) == mimetype


# --- o envio ---------------------------------------------------------------------------------


@pytest.fixture
def wiki() -> FakeWiki:
    return FakeWiki()


def refs(*sources: str) -> list[ImageRef]:
    return [ImageRef(src) for src in sources]


def test_envia_com_o_nome_do_hash_e_a_extensao_do_tipo(wiki: FakeWiki) -> None:
    foto, grafico = png(80, 60, seed=1), jpeg(200, 100)
    source = FakeSource(
        {
            "A/imagens/foto.png": ("image/png", foto),
            "A/imagens/grafico.jpeg": ("image/jpeg", grafico),
            "A/escondida.bin": ("application/octet-stream", gif(40, 40)),  # o ZIM não sabe o tipo
        }
    )

    urls = ImageUploader(wiki, source).upload(
        "A/artigo", refs("imagens/foto.png", "imagens/grafico.jpeg", "escondida.bin")
    )

    assert urls["imagens/foto.png"] == f"/ariadne/{sha(foto)[:2]}/{sha(foto)}.png"
    assert urls["imagens/grafico.jpeg"] == f"/ariadne/{sha(grafico)[:2]}/{sha(grafico)}.jpg"
    assert urls["escondida.bin"].endswith(".gif")
    assert set(wiki.assets) == {url.removeprefix("/") for url in urls.values()}
    assert wiki.assets[f"ariadne/{sha(foto)[:2]}/{sha(foto)}.png"] == ("image/png", foto)


def test_a_mesma_imagem_nunca_vai_duas_vezes(wiki: FakeWiki) -> None:
    mesma = png(64, 64, seed=3)
    source = FakeSource({"A/um.png": ("image/png", mesma), "B/dois.png": ("image/png", mesma)})
    uploader = ImageUploader(wiki, source)

    primeiro = uploader.upload("A/artigo", refs("um.png", "../B/dois.png"))
    segundo = ImageUploader(wiki, source).upload("A/outro", refs("um.png"))  # outro processo

    assert len(wiki.uploads) == 1  # o hash é o mesmo: dedupe dentro da corrida e entre corridas
    assert primeiro["um.png"] == primeiro["../B/dois.png"] == segundo["um.png"]


def test_resolve_o_src_face_ao_caminho_do_documento(wiki: FakeWiki) -> None:
    source = FakeSource(
        {
            "A/B/c.png": ("image/png", png(seed=1)),
            "A/x.png": ("image/png", png(seed=2)),
            "raiz.png": ("image/png", png(seed=3)),
            "A/B/com espaco.png": ("image/png", png(seed=4)),
        }
    )

    urls = ImageUploader(wiki, source).upload(
        "A/B/doc.html",
        refs(
            "c.png", "../x.png", "/raiz.png", "com%20espaco.png", "c.png?v=2", "../../../fora.png"
        ),
    )

    assert set(urls) == {"c.png", "../x.png", "/raiz.png", "com%20espaco.png", "c.png?v=2"}
    assert source.reads.count("A/B/c.png") == 2 and "../fora.png" not in source.reads


def test_ignora_icones_remotas_boilerplate_e_o_que_nao_e_imagem(wiki: FakeWiki) -> None:
    source = FakeSource(
        {
            "i/icone.png": ("image/png", png(16, 16)),
            "i/risca.png": ("image/png", png(300, 8)),
            "i/logo.png": ("image/png", png(100, 100, seed=9)),
            "i/boa.png": ("image/png", png(100, 100, seed=8)),
            "i/pagina.html": ("text/html", b"<html></html>"),
            "i/vazia.png": ("image/png", b""),
            "i/limite.png": ("image/png", png(32, 32, seed=7)),
        }
    )
    lixo = Boilerplate(images=frozenset({"logo.png"}))

    urls = ImageUploader(wiki, source).upload(
        "i/doc",
        refs(
            "icone.png",
            "risca.png",
            "../x/logo.png",
            "boa.png",
            "pagina.html",
            "vazia.png",
            "https://exemplo.org/a.png",
            "//cdn.exemplo.org/b.png",
            "limite.png",
            "",
        ),
        lixo,
    )

    assert set(urls) == {"boa.png", "limite.png"}  # 32 px não é «abaixo de 32»
    assert "i/logo.png" not in source.reads  # o lixo da fonte nem se lê
    assert len(wiki.uploads) == 2


def test_svg_nao_tem_tamanho_e_aceita_se(wiki: FakeWiki) -> None:
    source = FakeSource({"a.svg": ("image/svg+xml", SVG)})

    urls = ImageUploader(wiki, source).upload("doc", refs("a.svg"))

    assert urls["a.svg"].endswith(".svg") and wiki.assets[urls["a.svg"][1:]][0] == "image/svg+xml"


def test_data_uri_so_ate_1_mb(wiki: FakeWiki) -> None:
    import base64

    pequena = png(48, 48, seed=5)
    grande = png(1500, 400, seed=6) + secrets.token_bytes(1024 * 1024)
    uris = {
        "pequena": "data:image/png;base64," + base64.b64encode(pequena).decode(),
        "grande": "data:image/png;base64," + base64.b64encode(grande).decode(),
        "estragada": "data:image/png;base64,@@@@",
        "svg": "data:image/svg+xml," + SVG.decode().replace("<", "%3C").replace(">", "%3E"),
    }

    urls = ImageUploader(wiki, FakeSource({})).upload("doc", [ImageRef(u) for u in uris.values()])

    assert set(urls) == {uris["pequena"], uris["svg"]}
    assert len(wiki.uploads) == 2


def test_limite_de_tamanho_e_configuravel(wiki: FakeWiki) -> None:
    grande = png(200, 200, seed=2)
    source = FakeSource({"g.png": ("image/png", grande)})

    assert ImageUploader(wiki, source, max_bytes=len(grande) - 1).upload("d", refs("g.png")) == {}
    assert ImageUploader(wiki, source, min_side=201).upload("d", refs("g.png")) == {}
    assert ImageUploader(wiki, source, max_bytes=len(grande)).upload("d", refs("g.png"))


def test_um_erro_numa_imagem_nao_para_as_outras(
    wiki: FakeWiki, caplog: pytest.LogCaptureFixture
) -> None:
    ma, boa, outra = png(seed=1), png(seed=2), png(seed=3)
    wiki.fail_assets = {f"{sha(ma)}.png"}  # o Wiki.js recusa esta
    source = FakeSource(
        {
            "ma.png": ("image/png", ma),
            "boa.png": ("image/png", boa),
            "outra.png": ("image/png", outra),
        }
    )

    with caplog.at_level(logging.WARNING, logger="ariadne.wiki.assets"):
        urls = ImageUploader(wiki, source).upload(
            "d", refs("ma.png", "em-falta.png", "boa.png", "outra.png")
        )

    assert set(urls) == {"boa.png", "outra.png"}
    avisos = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 1 and "ma.png" in avisos[0] and "WikiError" in avisos[0]  # a ausente não


def test_um_ficheiro_ilegivel_tambem_fica_so_com_aviso(wiki: FakeWiki) -> None:
    class Partida(FakeSource):
        def read_bytes(self, path: str) -> tuple[str, bytes]:
            if path == "mau.png":
                raise RuntimeError("libzim rebentou")
            return super().read_bytes(path)

    source = Partida({"bom.png": ("image/png", png(seed=4))})

    urls = ImageUploader(wiki, source).upload("d", refs("mau.png", "bom.png"))

    assert set(urls) == {"bom.png"}


def test_wikijs_em_baixo_para_o_envio(wiki: FakeWiki) -> None:
    wiki.down = True
    source = FakeSource({"a.png": ("image/png", png())})

    with pytest.raises(WikiUnavailable):
        ImageUploader(wiki, source).upload("d", refs("a.png"))


def test_do_html_ao_markdown_com_as_imagens_enviadas(wiki: FakeWiki) -> None:
    html = (
        "<h2>Título</h2>"
        '<figure><img src="./_assets_/foto.jpeg" alt="Foto"><figcaption>Uma foto.</figcaption>'
        '</figure><p>Texto <img src="./_assets_/icone.png"> com ícone e<img src="lixo.png">.</p>'
    )
    foto = jpeg(300, 200)
    source = FakeSource(
        {
            "A/_assets_/foto.jpeg": ("image/jpeg", foto),
            "A/_assets_/icone.png": ("image/png", png(12, 12)),
        }
    )
    root, _ = convert.prepare(html)
    found = convert.images(root)

    urls = ImageUploader(wiki, source).upload("A/Artigo", found)
    markdown = convert.to_markdown(root, {}, urls)

    destino = f"/ariadne/{sha(foto)[:2]}/{sha(foto)}.jpg"
    assert markdown == f"## Título\n\n![Foto]({destino})\n\n*Uma foto.*\n\nTexto com ícone e."


# --- contra o Wiki.js do compose -------------------------------------------------------------


@pytest.fixture
def live(request: pytest.FixtureRequest) -> Iterator[WikiJsBackend]:
    """O Wiki.js do compose, ou salta o teste se não responde."""
    env = conftest.TestEnv()
    url = env.test_wikijs_url or f"http://localhost:{env.wikijs_port}"
    try:
        httpx.get(f"{url}/healthz", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Wiki.js do compose inacessível (docker compose up -d wikijs)")
    base, token = request.getfixturevalue("wikijs")
    backend = WikiJsBackend(base, token)
    yield backend
    backend.close()


def _delete_asset(backend: WikiJsBackend, folder: str, filename: str) -> None:
    """Retira o asset de teste (as pastas ficam: o Wiki.js 2.5 não as apaga por GraphQL)."""
    folder_id = backend._folder_id(folder)
    listing = backend._call(
        "query($f: Int!) { assets { list(folderId: $f, kind: ALL) { id filename } } }",
        {"f": folder_id},
    )
    for asset in listing["assets"]["list"]:
        if asset["filename"] == filename:
            backend._call(
                "mutation($id: Int!) { assets { deleteAsset(id: $id) { responseResult {"
                " succeeded } } } }",
                {"id": asset["id"]},
            )


@pytest.mark.db
def test_envio_real_para_o_wikijs(live: WikiJsBackend) -> None:
    imagem = png(48, 48, seed=secrets.randbelow(256)) + secrets.token_bytes(32)  # hash único
    nome = f"{sha(imagem)}.png"
    pasta = f"{assets.ROOT_FOLDER}/{sha(imagem)[:2]}"
    source = FakeSource({"A/foto.png": ("image/png", imagem)})
    uploader = ImageUploader(live, source)
    try:
        assert not live.asset_exists(f"{pasta}/{nome}")

        urls = uploader.upload("A/Artigo", refs("foto.png"))

        assert urls == {"foto.png": f"/{pasta}/{nome}"}
        assert live.asset_exists(f"{pasta}/{nome}")
        servida = httpx.get(live.url + urls["foto.png"])
        assert servida.status_code == 200
        assert servida.headers["content-type"].startswith("image/png")
        assert servida.content == imagem
        # outra corrida (outro processo): o HEAD diz que já lá está e não volta a enviar
        calls: list[str] = []
        original = live.upload_asset
        live.upload_asset = lambda *a, **k: calls.append("x") or original(*a, **k)  # type: ignore[method-assign]
        again = ImageUploader(live, source).upload("A/Artigo", refs("foto.png"))
        assert again == urls and calls == []
    finally:
        _delete_asset(live, pasta, nome)


@pytest.mark.db
def test_wikijs_normaliza_o_nome_do_ficheiro(live: WikiJsBackend) -> None:
    dados = png(40, 40, seed=secrets.randbelow(256)) + secrets.token_bytes(8)
    unico = secrets.token_hex(4)
    pasta = "ariadne-teste/imagens"
    url = live.upload_asset(pasta, f"Nome Com ESPAÇOS {unico}.PNG", dados, "image/png")
    try:
        assert url == f"/{pasta}/nome_com_espaços_{unico}.png"  # minúsculas e `_` nos espaços
        assert httpx.get(live.url + url).content == dados
        assert live.asset_exists(url.removeprefix("/"))
        assert not live.asset_exists(f"{pasta}/nao-existe-{unico}.png")
    finally:
        _delete_asset(live, pasta, f"nome_com_espaços_{unico}.png")


@pytest.mark.db
def test_wikijs_recusa_o_envio_sem_chave(live: WikiJsBackend) -> None:
    anonimo = WikiJsBackend(live.url, "")
    try:
        # 403 no envio, ou «Forbidden» logo na pasta (um Wiki.js novo não deixa o convidado ver os
        # assets): recusa nos dois casos.
        with pytest.raises(WikiError, match=r"403|Forbidden"):
            anonimo.upload_asset("ariadne/zz", "x.png", png(), "image/png")
    finally:
        anonimo.close()
