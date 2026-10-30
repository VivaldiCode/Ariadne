"""Para onde vai o conteúdo de uma release: um `tar.gz` em stream (o caminho normal: o texto claro
nunca chega a ser uma árvore no disco) ou uma pasta (só o `backup create --destination release`,
para o inspecionar; com permissões 0700/0600)."""

import io
import os
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Protocol

ARCHIVE_ROOT = "ariadne-release"


class Interrupted(Exception):
    """Pediram para parar a meio da construção (o worker está a parar)."""


class Sink(Protocol):
    def add_bytes(self, relative: str, data: bytes) -> None: ...

    def add_file(self, relative: str, path: Path) -> None: ...


class TarSink:
    """Escreve num `tarfile` aberto em modo stream (dono e permissões normalizados)."""

    def __init__(self, tar: tarfile.TarFile, check: Callable[[], None] | None = None) -> None:
        self.tar = tar
        self.check = check
        self.mtime = int(time.time())

    def _info(self, relative: str, size: int) -> tarfile.TarInfo:
        if self.check is not None:
            self.check()
        info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{relative}")
        info.size = size
        info.mtime = self.mtime
        info.mode = 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    def add_bytes(self, relative: str, data: bytes) -> None:
        self.tar.addfile(self._info(relative, len(data)), io.BytesIO(data))

    def add_file(self, relative: str, path: Path) -> None:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            self._add_stream(relative, handle, size)

    def _add_stream(self, relative: str, handle: IO[bytes], size: int) -> None:
        self.tar.addfile(self._info(relative, size), handle)


class DiskSink:
    """Escreve numa pasta, com pastas 0700 e ficheiros 0600 (é texto claro: só o dono o lê)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        root.chmod(0o700)

    def _target(self, relative: str) -> Path:
        target = self.root / relative
        for parent in reversed(target.relative_to(self.root).parents):
            folder = self.root / parent
            if not folder.exists():
                folder.mkdir(mode=0o700)
                folder.chmod(0o700)
        return target

    def add_bytes(self, relative: str, data: bytes) -> None:
        target = self._target(relative)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

    def add_file(self, relative: str, path: Path) -> None:
        self.add_bytes(relative, path.read_bytes())
