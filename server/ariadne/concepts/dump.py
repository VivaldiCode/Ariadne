"""Leitor, em *streaming*, dos dumps SQL do MySQL/MariaDB que a Wikimedia publica
(`wikidatawiki-latest-wb_items_per_site.sql.gz`, `ptwiki-latest-page.sql.gz`, …).

Um dump é um `CREATE TABLE` seguido de `INSERT INTO t VALUES (…),(…),…;` com linhas de vários
megabytes. Lê-se por blocos (sem carregar o ficheiro), tupla a tupla, e tolera o que os dumps
reais têm: aspas e barras escapadas (`\\'`, `\\\\`, `''`), `NULL`, números, `_binary '…'`, UTF-8
partido entre blocos e um ficheiro cortado a meio (a última tupla incompleta conta-se e ignora-se).
"""

import codecs
import re
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

CHUNK = 1 << 20  # 1 MiB por leitura
MAX_TUPLE = 1 << 20  # uma tupla maior do que isto já não é uma linha de dados

_STRING = r"'[^'\\]*(?:(?:\\.|'')[^'\\]*)*'"
_VALUE = rf"(?:_binary\s+)?{_STRING}|[^,()'\s]+"
_TUPLE = re.compile(rf"\(\s*(?:(?:{_VALUE})(?:\s*,\s*(?:{_VALUE}))*)?\s*\)", re.DOTALL)
_ITEM = re.compile(r"(?:_binary\s+)?'([^'\\]*(?:(?:\\.|'')[^'\\]*)*)'|([^,()'\s]+)", re.DOTALL)
_ESCAPE = re.compile(r"\\(.)|''", re.DOTALL)
_ESCAPES = {
    "0": "",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "Z": "\x1a",
    "%": "\\%",
    "_": "\\_",
}
_COLUMN = re.compile(r"^\s*`([^`]+)`\s", re.MULTILINE)


class DumpError(Exception):
    """O ficheiro não é um dump que se leia."""


@dataclass
class DumpStats:
    rows: int = 0
    bad_rows: int = 0  # tuplas que não se conseguiram ler (saltadas)
    truncated: bool = False  # o ficheiro acabou a meio de uma tupla


def unescape(raw: str) -> str:
    """O texto de uma string SQL do MySQL, sem as aspas."""
    if "\\" not in raw and "''" not in raw:
        return raw
    return _ESCAPE.sub(lambda m: _ESCAPES.get(m[1], m[1]) if m[1] else "'", raw)


def parse_tuple(inner: str) -> tuple[Any, ...]:
    """Os valores de uma tupla `(…)` já sem parênteses: inteiros, decimais, `None` e strings."""
    values: list[Any] = []
    for match in _ITEM.finditer(inner):
        quoted, token = match.groups()
        if quoted is not None:
            values.append(unescape(quoted))
        elif token.upper() == "NULL":
            values.append(None)
        else:
            try:
                values.append(int(token))
            except ValueError:
                try:
                    values.append(float(token))
                except ValueError:
                    values.append(token)  # 0x… e outros literais: ficam como texto
    return tuple(values)


class _GzipStream:
    """Lê um `.gz` por blocos sem perder o que já descomprimiu quando o ficheiro está cortado
    (`gzip.open` levanta `EOFError` e deita fora o bloco em curso). `truncated` diz se o fim do
    ficheiro chegou antes do fim dos dados comprimidos."""

    def __init__(self, path: Path) -> None:
        self._raw = path.open("rb")
        self._inflate = zlib.decompressobj(wbits=31)  # gzip
        self._buffer = b""
        self._done = False
        self.truncated = False

    def read(self, size: int = CHUNK) -> bytes:
        while len(self._buffer) < size and not self._done:
            block = self._raw.read(1 << 16)
            if not block:
                self._done = True
                self.truncated = not self._inflate.eof
                break
            try:
                self._buffer += self._inflate.decompress(block)
            except zlib.error:  # lixo a meio do ficheiro
                self._done = self.truncated = True
                break
            if self._inflate.eof:  # os dumps da Wikimedia têm um só membro gzip
                self._done = True
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def close(self) -> None:
        self._raw.close()

    def __enter__(self) -> "_GzipStream":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_dump(path: Path) -> IO[bytes]:
    """Abre um dump `.sql` ou `.sql.gz` em binário (um `.gz` cortado lê-se até onde der)."""
    if path.suffix == ".gz":
        return cast("IO[bytes]", _GzipStream(path))
    return path.open("rb")


class DumpTable:
    """As linhas de uma tabela de um dump. `columns` vem do `CREATE TABLE` (ou das omissões, se o
    dump não o traz); `stats` conta o que se leu."""

    def __init__(self, stream: IO[bytes], table: str, default_columns: Sequence[str] = ()) -> None:
        self._stream = stream
        self._table = table
        self.columns: list[str] = list(default_columns)
        self.stats = DumpStats()
        quoted = re.escape(table)
        self._create = re.compile(rf"CREATE\s+TABLE\s+(?:IF NOT EXISTS\s+)?`?{quoted}`?\s*\(")
        self._insert = re.compile(
            rf"INSERT\s+(?:IGNORE\s+)?INTO\s+`?{quoted}`?\s*(?:\([^)]*\)\s*)?VALUES\s*",
            re.IGNORECASE,
        )
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._eof = False

    def index(self, column: str) -> int:
        try:
            return self.columns.index(column)
        except ValueError as exc:
            raise DumpError(
                f"Table {self._table!r} has no column {column!r} (it has: {self.columns})."
            ) from exc

    # --- leitura -----------------------------------------------------------------------------

    def _fill(self) -> bool:
        """Acrescenta o bloco seguinte ao buffer; `False` se o ficheiro acabou."""
        if self._eof:
            return False
        try:
            data = self._stream.read(CHUNK)
        except (EOFError, OSError, zlib.error):
            # Um `.gz` cortado (descarregamento incompleto) rebenta a meio da descompressão: o que
            # já se leu conta, e o ficheiro fica marcado como truncado.
            data = b""
            self.stats.truncated = True
        if not data:
            self._eof = True
            if getattr(self._stream, "truncated", False):
                self.stats.truncated = True
            self._buf += self._decoder.decode(b"", final=True)
            return False
        self._buf += self._decoder.decode(data)
        return True

    def rows(self) -> Iterator[tuple[Any, ...]]:
        while True:
            kind = self._next_statement()
            if kind is None:
                return
            if kind == "create":
                self._read_columns()
            else:
                yield from self._read_values()

    def _locate(self) -> str | None:
        """Corta o buffer logo depois do próximo cabeçalho da tabela e diz de que tipo era."""
        create, insert = self._create.search(self._buf), self._insert.search(self._buf)
        found = [
            (m.start(), kind, m.end()) for kind, m in (("create", create), ("insert", insert)) if m
        ]
        if not found:
            return None
        _, kind, end = min(found)
        self._buf = self._buf[end:]
        return kind

    def _next_statement(self) -> str | None:
        """Avança até ao próximo `CREATE TABLE` ou `INSERT` da tabela; `None` no fim do ficheiro."""
        keep = 256  # um cabeçalho cortado entre dois blocos ainda se encontra
        while True:
            kind = self._locate()
            if kind is not None:
                return kind
            self._buf = self._buf[-keep:]
            if not self._fill():
                return self._locate()

    def _read_columns(self) -> None:
        while True:
            end = self._buf.find("\n)")
            if end >= 0:
                break
            if not self._fill():
                end = len(self._buf)
                break
        columns = _COLUMN.findall("\n" + self._buf[:end])
        if columns:
            self.columns = columns
        self._buf = self._buf[end:]

    def _read_values(self) -> Iterator[tuple[Any, ...]]:
        """As tuplas de um `INSERT … VALUES`, até ao `;` (o buffer começa no fim do cabeçalho)."""
        pos = 0
        while True:
            buf = self._buf
            while pos < len(buf) and buf[pos] in " \t\r\n,":  # entre tuplas
                pos += 1
            if pos >= len(buf):
                self._buf, pos = "", 0
                if not self._fill() and not self._buf:
                    return
                continue
            if buf[pos] == ";":
                self._buf = buf[pos + 1 :]
                return
            match = _TUPLE.match(buf, pos) if buf[pos] == "(" else None
            if match is not None:
                self.stats.rows += 1
                yield parse_tuple(match.group(0)[1:-1])
                pos = match.end()
                continue
            # Não bateu: ou a tupla ainda está a chegar, ou está estragada.
            if not self._eof and len(buf) - pos < MAX_TUPLE:
                self._buf, pos = buf[pos:], 0
                self._fill()
                continue
            self.stats.bad_rows += 1
            following = self._next_boundary(buf, pos)
            if following is None:
                self._buf = ""
                self.stats.truncated = self._eof
                if self._eof or not self._fill():
                    return
                pos = 0
                continue
            pos = following

    @staticmethod
    def _next_boundary(buf: str, pos: int) -> int | None:
        """Onde começa a tupla (ou o `;`) a seguir à que está estragada em `pos`."""
        starts = [at + 2 for at in [buf.find("),(", pos + 1)] if at >= 0]
        starts += [at + 1 for at in [buf.find(");", pos + 1)] if at >= 0]
        return min(starts) if starts else None


def iter_table(stream: IO[bytes], table: str, default_columns: Sequence[str] = ()) -> DumpTable:
    return DumpTable(stream, table, default_columns)
