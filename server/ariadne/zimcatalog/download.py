"""Descarga de um ZIM para `ZIM_DIR`: `.part` + rename, retoma por Range, tamanho e sha-256.

Os dados vêm do `.meta4` (Metalink) que o catálogo indica: nome do ficheiro, tamanho exato, sha-256
e os espelhos por ordem de preferência. Um `.part` interrompido retoma-se do ponto onde ficou
(pedido `Range`); no fim confere-se o tamanho e o sha-256 e só então o ficheiro ganha o nome
final (`os.replace`), por isso o kiwix e o registo de fontes nunca veem um ZIM a meio. Os URL
vêm sempre do catálogo, mas cada salto (espelhos e redirecionamentos) é verificado: só http(s) e
nunca endereços privados/locais (SSRF), a não ser que o próprio catálogo seja local.
"""

import errno
import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 6
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.zim")
SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_M4 = "{urn:ietf:params:xml:ns:metalink}"
MAX_META_BYTES = 2 * 1024 * 1024


class DownloadError(Exception):
    """A descarga falhou (rede, tamanho ou checksum errados, disco cheio)."""


class FatalDownloadError(DownloadError):
    """Sem remédio por repetir (disco cheio, o servidor envia mais do que prometeu, sem
    checksum): não se tenta de novo e o `.part` apaga-se."""


class DownloadCancelled(Exception):
    """Pediram para parar (cancelamento ou paragem do worker); o `.part` fica para retomar."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason or "download interrupted")


@dataclass
class DownloadMeta:
    filename: str
    size: int | None
    sha256: str | None
    urls: list[str] = field(default_factory=list)


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def pin_url(url: str, *, allow_local: bool) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Valida `url` e devolve `(url a pedir, cabeçalhos, extensões)` com o **IP já validado
    fixado** na ligação: só http(s) e, salvo `allow_local`, nunca um host que resolva para um
    endereço não global. O pedido vai ao IP verificado (com `Host` e SNI do nome original, por
    isso o certificado continua a ser o do nome), e o nome não volta a ser resolvido: sem DNS
    rebinding entre a verificação e a ligação."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DownloadError(f"URL refused (http/https only): {url[:120]}")
    if allow_local:
        return url, {}, {}
    if parts.scheme != "https":
        raise DownloadError(f"URL refused (https required): {url[:120]}")
    host = parts.hostname
    try:
        addresses = [
            str(info[4][0])
            for info in socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
        ]
    except OSError as exc:
        raise DownloadError(f"cannot resolve host {host}: {exc}") from exc
    if not addresses:
        raise DownloadError(f"cannot resolve host {host}")
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise DownloadError(f"URL refused (host {host} resolves to a local address)")
    ip = addresses[0].split("%")[0]
    literal = False
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        pass
    if literal:
        return url, {}, {}
    netloc = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    host_header = host if not parts.port else f"{host}:{parts.port}"
    return parts._replace(netloc=netloc).geturl(), {"Host": host_header}, {"sni_hostname": host}


def check_url(url: str, *, allow_local: bool) -> None:
    pin_url(url, allow_local=allow_local)


def parse_meta4(data: bytes, fallback_url: str | None = None) -> DownloadMeta:
    if len(data) > MAX_META_BYTES or b"<!doctype" in data.lower() or b"<!entity" in data.lower():
        raise DownloadError("the catalog .meta4 is invalid")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise DownloadError(f"the catalog .meta4 is invalid ({exc})") from exc
    node = root.find(f"{_M4}file")
    if node is None:
        raise DownloadError("the .meta4 does not describe any file")
    filename = node.get("name", "")
    if not FILENAME.fullmatch(filename):
        raise DownloadError(f"file name refused in the .meta4: {filename[:80]!r}")
    size_node = node.find(f"{_M4}size")
    size_text = (size_node.text or "").strip() if size_node is not None else ""
    size = int(size_text) if size_text.isdigit() else None
    sha = None
    for hash_node in node.findall(f"{_M4}hash"):
        value = (hash_node.text or "").strip()
        if hash_node.get("type") == "sha-256" and SHA256.fullmatch(value):
            sha = value.lower()
    urls = sorted(
        (
            (int(u.get("priority") or 999), (u.text or "").strip())
            for u in node.findall(f"{_M4}url")
            if (u.text or "").strip()
        ),
        key=lambda pair: pair[0],
    )
    ordered = [url for _, url in urls]
    if fallback_url and fallback_url not in ordered:
        ordered.append(fallback_url)
    return DownloadMeta(filename=filename, size=size, sha256=sha, urls=ordered)


def resolve_meta(client: httpx.Client, download_url: str, *, allow_local: bool) -> DownloadMeta:
    """O que se vai descarregar: do `.meta4`, ou (URL direto de um .zim) do nome e do `.sha256`
    ao lado, quando existe."""
    check_url(download_url, allow_local=allow_local)
    if download_url.split("?")[0].endswith(".meta4"):
        return parse_meta4(_get_small(client, download_url, allow_local=allow_local))
    filename = urlparse(download_url).path.rsplit("/", 1)[-1]
    if not FILENAME.fullmatch(filename):
        raise DownloadError(f"file name refused: {filename[:80]!r}")
    sha = None
    try:
        text = _get_small(client, download_url + ".sha256", allow_local=allow_local).decode(
            "utf-8", errors="replace"
        )
        first = text.split()[0] if text.split() else ""
        sha = first.lower() if SHA256.fullmatch(first) else None
    except DownloadError:
        sha = None
    return DownloadMeta(filename=filename, size=None, sha256=sha, urls=[download_url])


def _get_small(client: httpx.Client, url: str, *, allow_local: bool) -> bytes:
    """GET de um ficheiro pequeno (`.meta4`, `.sha256`), lido por blocos e com limite de tamanho."""
    for _ in range(MAX_REDIRECTS + 1):
        request_url, headers, extensions = pin_url(url, allow_local=allow_local)
        try:
            with client.stream(
                "GET", request_url, headers=headers, extensions=extensions, follow_redirects=False
            ) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code >= 400:
                    raise DownloadError(f"{url[:100]}: HTTP {response.status_code}")
                data = bytearray()
                for chunk in response.iter_raw():
                    data += chunk
                    if len(data) > MAX_META_BYTES:
                        raise DownloadError(f"{url[:100]}: response too large")
                return bytes(data)
        except httpx.HTTPError as exc:
            raise DownloadError(f"{url[:100]}: {type(exc).__name__}") from exc
    raise DownloadError("too many redirects")


Progress = Callable[[int, int | None, str], None]


def _sha256_of(path: Path, progress: Progress, total: int | None) -> str:
    digest = hashlib.sha256()
    done = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK * 4):
            digest.update(chunk)
            done += len(chunk)
            progress(done, total, "verifying")
    return digest.hexdigest()


def _open_ok(path: Path) -> None:
    """O ficheiro abre como ZIM (cabeçalho legível)? Apanha lixo com o checksum em falta."""
    from libzim.reader import Archive

    try:
        Archive(path)
    except (RuntimeError, OSError) as exc:
        raise DownloadError(f"the downloaded file is not a valid ZIM ({exc})") from exc


def _stream(
    client: httpx.Client,
    url: str,
    part: Path,
    have: int,
    total: int | None,
    *,
    limit: int | None,
    allow_local: bool,
    progress: Progress,
    should_stop: Callable[[], str | None],
) -> int:
    """Um pedido (com Range se há `.part`), a seguir os redirecionamentos um a um. `limit` é o
    tamanho prometido (do `.meta4`): se o servidor enviar mais, aborta-se já. Devolve os bytes que
    o `.part` tem no fim."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        request_url, pinned, extensions = pin_url(current, allow_local=allow_local)
        headers = {"Accept-Encoding": "identity", **pinned}
        if have:
            headers["Range"] = f"bytes={have}-"
        with client.stream(
            "GET", request_url, headers=headers, extensions=extensions, follow_redirects=False
        ) as response:
            if response.is_redirect and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            if response.status_code == 416 and have:  # o .part já tem tudo (ou mais): confere-se
                return have
            if response.status_code not in (200, 206):
                raise DownloadError(f"{current[:100]}: HTTP {response.status_code}")
            if response.status_code == 200 and have:  # ignorou o Range: recomeça-se
                have = 0
            mode = "ab" if have else "wb"
            if total is None:
                length = response.headers.get("content-length")
                total = (int(length) + have) if length and length.isdigit() else None
            if limit is not None and total is not None and total > limit:
                raise FatalDownloadError(
                    f"the server announces {total} bytes and the .meta4 promised {limit}"
                )
            with part.open(mode) as out:
                # Sem agrupar: o que chegou antes de a ligação cair já está no .part.
                for chunk in response.iter_raw():
                    if limit is not None and have + len(chunk) > limit:
                        raise FatalDownloadError(
                            f"wrong size: the server sent more than the {limit} bytes given"
                        )
                    try:
                        out.write(chunk)
                    except OSError as exc:
                        if exc.errno == errno.ENOSPC:
                            raise FatalDownloadError("disk full while writing the .part") from exc
                        raise
                    have += len(chunk)
                    progress(have, total, "downloading")
                    reason = should_stop()
                    if reason:
                        out.flush()
                        raise DownloadCancelled(reason)
                out.flush()
                os.fsync(out.fileno())
            return have
    raise DownloadError("too many redirects")


def download_zim(
    meta: DownloadMeta,
    dest_dir: Path,
    *,
    client: httpx.Client,
    allow_local: bool = False,
    progress: Progress | None = None,
    should_stop: Callable[[], str | None] | None = None,
    free_margin: int = 512 * 1024 * 1024,
    sleep: Callable[[float], None] = time.sleep,
    allow_unverified: bool = False,
) -> Path:
    """Descarrega para `dest_dir/<nome>.zim` (devolve o caminho). Idempotente: se o ficheiro final
    já existe com o tamanho e o checksum certos, não descarrega nada. Sem sha-256 (os espelhos são
    de terceiros) recusa, a não ser que um administrador o permita (`allow_unverified`)."""
    progress = progress or (lambda done, total, phase: None)
    should_stop = should_stop or (lambda: None)
    if not FILENAME.fullmatch(meta.filename):
        raise DownloadError(f"file name refused: {meta.filename[:80]!r}")
    if meta.sha256 is None and not allow_unverified:
        raise FatalDownloadError(
            "the catalog gives no sha-256 for this file and the mirrors are third-party: refused "
            "(an admin can allow the download without a checksum, at their own risk)"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / meta.filename
    part = dest_dir / f"{meta.filename}.part"
    if final.exists():
        ok = meta.size is None or final.stat().st_size == meta.size
        if ok and meta.sha256:
            ok = _sha256_of(final, progress, meta.size) == meta.sha256
        if ok:
            return final
        raise DownloadError(f"{meta.filename} already exists and is different: not replacing it")

    try:
        return _download(
            meta,
            dest_dir,
            final,
            part,
            client,
            allow_local,
            progress,
            should_stop,
            free_margin,
            sleep,
        )
    except FatalDownloadError:
        part.unlink(missing_ok=True)  # sem remédio: não fica lixo
        raise


def _download(
    meta: DownloadMeta,
    dest_dir: Path,
    final: Path,
    part: Path,
    client: httpx.Client,
    allow_local: bool,
    progress: Progress,
    should_stop: Callable[[], str | None],
    free_margin: int,
    sleep: Callable[[float], None],
) -> Path:
    errors: list[str] = []
    complete = False
    for attempt in range(MAX_ATTEMPTS):
        have = part.stat().st_size if part.exists() else 0
        if meta.size is not None and have > meta.size:
            part.unlink()
            have = 0
        if meta.size is not None and have == meta.size:
            complete = True
            break
        if meta.size is not None:
            free = shutil.disk_usage(dest_dir).free
            if free < meta.size - have + free_margin:
                raise FatalDownloadError(
                    f"not enough disk space: {meta.size - have} bytes needed and {free} "
                    f"free (margin {free_margin})"
                )
        url = meta.urls[attempt % len(meta.urls)]
        try:
            _stream(
                client,
                url,
                part,
                have,
                meta.size,
                limit=meta.size,
                allow_local=allow_local,
                progress=progress,
                should_stop=should_stop,
            )
        except FatalDownloadError:
            raise
        except (httpx.HTTPError, DownloadError) as exc:
            errors.append(f"{urlparse(url).hostname}: {exc}")
            logger.warning("Download of %s failed at %s: %s", meta.filename, url[:80], exc)
            sleep(min(2.0**attempt, 30.0))
            continue
        if meta.size is not None and part.stat().st_size > meta.size:
            got = part.stat().st_size
            part.unlink()
            raise FatalDownloadError(f"wrong size: {got} bytes instead of {meta.size}")
        if meta.size is None:  # sem tamanho conhecido, um pedido que acaba bem é o ficheiro todo
            complete = True
            break
    if not complete:
        have = part.stat().st_size if part.exists() else 0
        if meta.size is not None and have == meta.size:
            complete = True
    if not complete:
        raise DownloadError(
            f"incomplete download ({part.stat().st_size if part.exists() else 0}"
            f"/{meta.size} bytes); the .part is kept to resume. " + "; ".join(errors[-3:])
        )

    size = part.stat().st_size
    if meta.size is not None and size != meta.size:
        part.unlink(missing_ok=True)
        raise DownloadError(f"wrong size: {size} bytes instead of {meta.size}")
    if meta.sha256:
        digest = _sha256_of(part, progress, size)
        if digest != meta.sha256:
            part.unlink(missing_ok=True)
            raise DownloadError("the sha-256 does not match (corrupted file); discarding it")
    else:
        _open_ok(part)
    os.replace(part, final)
    progress(size, size, "done")
    return final
