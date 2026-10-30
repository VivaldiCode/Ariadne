"""Envio das imagens de um artigo para os assets do Wiki.js (WIKI-IMPORT.md, secção 3).

Cada imagem lê-se do próprio ZIM (o sistema é offline: `http(s)` não se busca), fica com o nome do
hash do conteúdo, `ariadne/<aa>/<sha256>.<ext>`, e só vai se o Wiki.js ainda não a tem. A mesma
imagem referida por duas páginas (ou por dois `src` diferentes) nunca vai duas vezes.
"""

import base64
import binascii
import hashlib
import logging
import posixpath
import re
import struct
from collections.abc import Sequence
from typing import Protocol
from urllib.parse import unquote, urlsplit

from ariadne.sources.base import NotFound
from ariadne.wiki.base import WikiBackend, WikiUnavailable
from ariadne.wiki.boilerplate import Boilerplate
from ariadne.wiki.convert import ImageRef

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024  # o limite de envio do Wiki.js
MAX_DATA_URI = 1024 * 1024  # `data:` maior que isto é quase sempre um erro de exportação
MIN_SIDE = 32  # abaixo disto são ícones, marcadores e linhas decorativas
ROOT_FOLDER = "ariadne"

_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/avif": "avif",
}
_REMOTE = re.compile(r"^(?:https?:)?//", re.IGNORECASE)
_DATA_URI = re.compile(r"^data:([^;,]*)((?:;[^;,]+)*),(.*)$", re.DOTALL | re.IGNORECASE)


class BytesSource(Protocol):
    """O que o envio precisa da fonte: ler uma entrada em bruto (`ZimSource.read_bytes`)."""

    def read_bytes(self, path: str) -> tuple[str, bytes]: ...


def sniff_mimetype(data: bytes) -> str:
    """O tipo da imagem pelos primeiros bytes (para o ZIM que dá `application/octet-stream`)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (
        head.startswith((b"<?xml", b"<!--")) and b"<svg" in data[:2048]
    ):
        return "image/svg+xml"
    return ""


def image_size(data: bytes) -> tuple[int, int] | None:
    """(largura, altura) lidas do cabeçalho de PNG, GIF, JPEG ou WebP; `None` se não se sabe
    (SVG, formato desconhecido ou cabeçalho truncado)."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data.startswith((b"GIF87a", b"GIF89a")):
            return struct.unpack("<HH", data[6:10])
        if data.startswith(b"\xff\xd8"):
            return _jpeg_size(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp_size(data)
    except struct.error:
        return None
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    position = 2
    while position + 9 < len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            position += 1 if marker == 0xFF else 2
            continue
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # SOF: quadro da imagem
            height, width = struct.unpack(">HH", data[position + 5 : position + 9])
            return width, height
        position += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    kind = data[12:16]
    if kind == b"VP8 ":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"VP8L":
        (bits,) = struct.unpack("<I", data[21:25])
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None


def _data_uri(src: str) -> tuple[str, bytes] | None:
    """(tipo, bytes) de um `data:` que cabe no limite; `None` se é grande ou está estragado."""
    match = _DATA_URI.match(src)
    if match is None or len(match.group(3)) > MAX_DATA_URI * 4 // 3 + 4:
        return None
    mimetype, params, payload = match.group(1).lower(), match.group(2).lower(), match.group(3)
    try:
        data = (
            base64.b64decode(payload, validate=False)
            if ";base64" in params
            else unquote(payload).encode("utf-8")
        )
    except (binascii.Error, ValueError):
        return None
    return (mimetype, data) if len(data) <= MAX_DATA_URI else None


class ImageUploader:
    """Envia as imagens de um documento para o Wiki.js e diz o URL de cada uma."""

    def __init__(
        self,
        wiki: WikiBackend,
        source: BytesSource,
        *,
        max_bytes: int = MAX_BYTES,
        min_side: int = MIN_SIDE,
    ) -> None:
        self.wiki = wiki
        self.source = source
        self.max_bytes = max_bytes
        self.min_side = min_side
        self._known: dict[str, str] = {}  # sha256 -> URL, para não perguntar duas vezes ao Wiki.js

    def upload(
        self,
        document_path: str,
        refs: Sequence[ImageRef],
        boilerplate: Boilerplate | None = None,
    ) -> dict[str, str]:
        """`src` -> URL no Wiki.js, só para as imagens que ficaram lá. Fora ficam as remotas, os
        ícones, as do lixo da fonte, as grandes demais e as que não se conseguiu ler: cada uma
        regista um aviso e as outras seguem. Só o Wiki.js em baixo pára o envio (a página
        também falharia, e o importador tenta mais tarde)."""
        urls: dict[str, str] = {}
        for ref in refs:
            if ref.src in urls:
                continue
            try:
                url = self._one(document_path, ref.src, boilerplate)
            except WikiUnavailable:
                raise
            except NotFound:  # os ZIM «nopic» não trazem as imagens: é o normal, não um aviso
                log.debug("imagem %r de %s não está no ZIM", ref.src[:80], document_path)
                continue
            except Exception as exc:  # uma imagem má não derruba o artigo
                log.warning("image %r from %s not uploaded: %s: %s", ref.src[:80], document_path,
                            type(exc).__name__, exc)  # fmt: skip
                continue
            if url:
                urls[ref.src] = url
        return urls

    def _read(self, document_path: str, src: str) -> tuple[str, bytes] | None:
        if src.startswith("data:"):
            return _data_uri(src)
        if _REMOTE.match(src):
            return None  # o sistema é offline
        target = unquote(urlsplit(src).path)
        if not target:
            return None
        if target.startswith("/"):
            target = target.lstrip("/")
        else:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(document_path), target))
        if target in (".", "..") or target.startswith("../"):
            return None
        return self.source.read_bytes(target)

    def _one(self, document_path: str, src: str, boilerplate: Boilerplate | None) -> str | None:
        if not src or (boilerplate is not None and boilerplate.is_image(src)):
            return None
        found = self._read(document_path, src)
        if found is None:
            return None
        mimetype, data = found
        if not data or len(data) > self.max_bytes:
            return None
        if mimetype not in _EXTENSIONS:
            mimetype = sniff_mimetype(data)
        if mimetype not in _EXTENSIONS:
            return None  # não é uma imagem que o Wiki.js sirva
        size = image_size(data)
        if size is not None and min(size) < self.min_side:
            return None
        sha = hashlib.sha256(data).hexdigest()
        if (known := self._known.get(sha)) is not None:
            return known
        folder, filename = f"{ROOT_FOLDER}/{sha[:2]}", f"{sha}.{_EXTENSIONS[mimetype]}"
        if self.wiki.asset_exists(f"{folder}/{filename}"):
            url = f"/{folder}/{filename}"
        else:
            url = self.wiki.upload_asset(folder, filename, data, mimetype)
        self._known[sha] = url
        return url
