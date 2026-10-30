"""Empacotar a release: tar.gz do conteúdo -> age -> partes de no máximo 2 GB -> manifesto.

O texto claro não é uma árvore no disco: o conteúdo escreve-se como `tar.gz` em stream para o stdin
do `age` (só passa pelo pipe e pela memória do processo), e uma thread lê o cifrado do stdout e
parte-o em ficheiros `…​.age.001`, `.002`…, calculando o sha-256 de
cada parte. O `MANIFEST.json` (tamanhos e impressões digitais das partes e do conteúdo), o
`SHA256SUMS` (para `sha256sum -c`) e o `restore.sh` ficam ao lado, em claro: nada disso revela o
conteúdo (só contagens).
"""

import contextlib
import hashlib
import json
import tarfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import IO, Any

from ariadne.release.age import HEADER, Age, AgeError, check_recipient
from ariadne.release.sign import sign_file
from ariadne.release.sink import Sink, TarSink

MAX_PART_BYTES = 2_000_000_000  # abaixo dos 2 GiB que o GitHub aceita por ficheiro
MANIFEST = "MANIFEST.json"
SIGNATURE = "MANIFEST.json.sig"
SUMS = "SHA256SUMS"
RESTORE = "restore.sh"
READ_CHUNK = 1024 * 1024


@dataclass
class PartInfo:
    name: str
    bytes: int
    sha256: str


@dataclass
class Packed:
    base: str
    parts: list[PartInfo] = field(default_factory=list)
    content_bytes: int = 0
    content_sha256: str = ""
    cipher_bytes: int = 0
    cipher_sha256: str = ""
    header_ok: bool = False
    canary_in_clear: bool = False


class _HashingWriter:
    """Escreve no stdin do age e mede o texto claro que passa (tamanho e sha-256)."""

    def __init__(self, sink: IO[bytes]) -> None:
        self.sink = sink
        self.digest = hashlib.sha256()
        self.bytes = 0

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        self.bytes += len(data)
        self.sink.write(data)
        return len(data)

    def flush(self) -> None:
        self.sink.flush()


class _Splitter:
    def __init__(self, folder: Path, base: str, part_bytes: int, canary: bytes) -> None:
        self.folder, self.base, self.part_bytes = folder, base, part_bytes
        self.canary = canary
        self.parts: list[PartInfo] = []
        self.total = hashlib.sha256()
        self.total_bytes = 0
        self.first = b""
        self.canary_seen = False
        self._tail = b""
        self.error: BaseException | None = None

    def consume(self, stream: IO[bytes]) -> None:
        try:
            self._consume(stream)
        except BaseException as exc:
            self.error = exc
            while stream.read(READ_CHUNK):  # esvazia, para o age não ficar preso a escrever
                pass

    def _consume(self, stream: IO[bytes]) -> None:
        out: IO[bytes] | None = None
        digest = hashlib.sha256()
        written = 0
        name = ""
        while chunk := stream.read(READ_CHUNK):
            if len(self.first) < len(HEADER):
                self.first = (self.first + chunk)[: len(HEADER)]
            self.total.update(chunk)
            self.total_bytes += len(chunk)
            window = self._tail + chunk  # o texto claro nunca deve aparecer no cifrado
            if self.canary and self.canary in window:
                self.canary_seen = True
            self._tail = window[-(len(self.canary) + 8) :] if self.canary else b""
            view = memoryview(chunk)
            while len(view):
                if out is None:
                    if len(self.parts) >= 999:
                        raise AgeError("Too many parts (999): increase RELEASE_PART_BYTES.")
                    name = f"{self.base}.{len(self.parts) + 1:03d}"
                    out = (self.folder / name).open("wb")
                    digest, written = hashlib.sha256(), 0
                piece = view[: self.part_bytes - written]
                out.write(piece)
                digest.update(piece)
                written += len(piece)
                view = view[len(piece) :]
                if written >= self.part_bytes:
                    out.close()
                    self.parts.append(PartInfo(name, written, digest.hexdigest()))
                    out = None
        if out is not None:
            out.close()
            self.parts.append(PartInfo(name, written, digest.hexdigest()))


def pack[T](
    produce: Callable[[Sink], T],
    out_dir: Path,
    *,
    age: Age,
    recipient: str,
    part_bytes: int,
    base: str,
    canary: str,
    check: Callable[[], None] | None = None,
) -> tuple[Packed, T]:
    """Corre `produce(sink)` com o sink ligado a `tar.gz` -> `age` -> partes em `out_dir`: o texto
    claro nunca é uma árvore no disco (só passa pelo pipe do `age`). `check` é chamado a cada
    ficheiro (para parar a meio)."""
    if not 0 < part_bytes <= MAX_PART_BYTES:
        raise AgeError(f"RELEASE_PART_BYTES must be between 1 and {MAX_PART_BYTES} (2 GB).")
    key = check_recipient(recipient)
    out_dir.mkdir(parents=True, exist_ok=True)
    splitter = _Splitter(out_dir, base, part_bytes, canary.encode())
    proc = age.start_encrypt(key)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    reader = threading.Thread(target=splitter.consume, args=(proc.stdout,), daemon=True)
    reader.start()
    writer = _HashingWriter(proc.stdin)
    failure: BaseException | None = None
    produced: T | None = None
    try:
        with tarfile.open(fileobj=writer, mode="w|gz", format=tarfile.PAX_FORMAT) as tar:  # type: ignore[call-overload]
            produced = produce(TarSink(tar, check))
    except BaseException as exc:
        failure = exc
    finally:
        with contextlib.suppress(BrokenPipeError):
            proc.stdin.close()
    reader.join()
    stderr = proc.stderr.read().decode(errors="replace")
    code = proc.wait()
    if failure is not None:
        raise failure
    if splitter.error is not None:
        raise splitter.error
    if code != 0:
        raise AgeError(f"age failed (code {code}): {stderr[:300]}")
    packed = Packed(
        base=base,
        parts=splitter.parts,
        content_bytes=writer.bytes,
        content_sha256=writer.digest.hexdigest(),
        cipher_bytes=splitter.total_bytes,
        cipher_sha256=splitter.total.hexdigest(),
        header_ok=splitter.first.startswith(HEADER),
        canary_in_clear=splitter.canary_seen,
    )
    return packed, produced  # type: ignore[return-value]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def restore_script() -> str:
    """O `restore.sh` com o esquema de restauro (`schema.sql`) embutido: o arquivo só traz dados."""
    template = resources.files("ariadne.release").joinpath("restore.sh").read_text(encoding="utf-8")
    schema = resources.files("ariadne.release").joinpath("schema.sql").read_text(encoding="utf-8")
    return template.replace("@@SCHEMA_SQL@@", schema.strip())


def write_manifest(
    out_dir: Path, packed: Packed, info: dict[str, Any], *, recipient: str, signing_key: Path
) -> tuple[Path, str]:
    """Escreve `MANIFEST.json` (assinado: `MANIFEST.json.sig`), `SHA256SUMS` e `restore.sh`.
    Devolve o manifesto e o seu sha-256. O manifesto leva o sha-256 do `restore.sh`, por isso a
    assinatura cobre-o também."""
    script = restore_script()
    manifest = {
        "format": "ariadne-release-manifest/1",
        **info,
        "encryption": {
            "tool": "age",
            "recipient": check_recipient(recipient),
            "header": HEADER.decode(),
            "note": "The private key was never on the system that produced this release.",
        },
        "content_name": packed.base.removesuffix(".age"),
        "content_bytes": packed.content_bytes,
        "content_sha256": packed.content_sha256,
        "cipher_bytes": packed.cipher_bytes,
        "cipher_sha256": packed.cipher_sha256,
        "part_max_bytes": info.get("part_max_bytes"),
        "restore_sh_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "parts": [{"name": p.name, "bytes": p.bytes, "sha256": p.sha256} for p in packed.parts],
        "checks": {"age_header": packed.header_ok, "plaintext_in_parts": packed.canary_in_clear},
    }
    path = out_dir / MANIFEST
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sign_file(path, signing_key)
    manifest_sha = sha256_file(path)
    restore = out_dir / RESTORE
    restore.write_text(script, encoding="utf-8")
    restore.chmod(0o755)
    sums = [f"{p.sha256}  {p.name}" for p in packed.parts] + [
        f"{manifest_sha}  {MANIFEST}",
        f"{sha256_file(out_dir / SIGNATURE)}  {SIGNATURE}",
    ]
    (out_dir / SUMS).write_text("\n".join(sums) + "\n", encoding="utf-8")
    return path, manifest_sha
