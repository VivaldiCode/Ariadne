"""Vetores de artigo (ARCHITECTURE.md, secção 7.1): um vetor por artigo, de título + 1.º parágrafo.

Servem para descobrir ligações entre fontes (`linker`) e como vizinhança semântica; não substituem
os vetores de chunk do nível 3. A carga inicial faz-se no Mac (`ariadne vectors build --out`),
escreve-se Parquet e importa-se no servidor (`ariadne vectors import`); o servidor só gera, por
job `article_vector`, os dos documentos que o uso foi tocando.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Source as SourceRow
from ariadne.documents import MAX_ALIASES, add_aliases, make_document_id
from ariadne.embedder import EMBEDDING_DIM, Embedder
from ariadne.relink import relink_when
from ariadne.semantic import SemanticStore
from ariadne.sources.base import Article, Source, SourceError
from ariadne.textutil import clean_text, title_key

logger = logging.getLogger(__name__)

# Um parágrafo de introdução tem de ter alguma substância; o resto do texto é demais para o modelo.
MIN_PARAGRAPH_CHARS = 40
MAX_PASSAGE_CHARS = 800
PARQUET_BATCH = 1000  # linhas lidas de cada vez
ROW_GROUP = 10_000  # linhas por grupo ao escrever


class VectorsError(Exception):
    """Ficheiro ou pedido que não se pode usar; a mensagem é para quem corre o comando."""


@dataclass(frozen=True)
class ArticleVector:
    source_id: str
    path: str
    title: str
    language: str | None
    version: str
    model_id: str
    embedding: list[float]
    aliases: tuple[str, ...] = ()

    @property
    def document_id(self) -> str:
        return make_document_id(self.source_id, self.path)


# --- o texto do artigo ---------------------------------------------------------------------------


def first_paragraph(article: Article) -> str:
    """O 1.º parágrafo com substância: o da introdução (antes do 1.º título), senão o 1.º do
    artigo, senão o 1.º bloco de texto que houver."""
    intro_end = next(
        (i for i, b in enumerate(article.blocks) if b.kind == "h"), len(article.blocks)
    )
    for blocks in (article.blocks[:intro_end], article.blocks):
        for block in blocks:
            if block.kind == "p" and len(block.text) >= MIN_PARAGRAPH_CHARS:
                return block.text
    return next((b.text for b in article.blocks if b.kind != "h"), "")


def passage_text(article: Article) -> str:
    """O que se vetoriza: o título e o 1.º parágrafo (cortado em MAX_PASSAGE_CHARS, de preferência
    no fim de uma palavra)."""
    paragraph = first_paragraph(article)
    if len(paragraph) > MAX_PASSAGE_CHARS:
        cut = paragraph.rfind(" ", 0, MAX_PASSAGE_CHARS)
        paragraph = paragraph[: cut if cut > MAX_PASSAGE_CHARS // 2 else MAX_PASSAGE_CHARS]
    return f"{article.title}\n{paragraph}" if paragraph else article.title


def collect_aliases(source: Source) -> dict[str, list[str]]:
    """Caminho canónico -> títulos alternativos (os redirects da fonte), sem os iguais ao título
    do artigo; a comparação é a do `title_match`."""
    found: dict[str, list[str]] = {}
    for title, path in source.redirects():
        titles = found.setdefault(path, [])
        if len(titles) < MAX_ALIASES and title not in titles:  # o tecto vale já na recolha
            titles.append(title)
    return found


# --- gerar ---------------------------------------------------------------------------------------


def iter_article_vectors(
    source: Source,
    embedder: Embedder,
    *,
    limit: int | None = None,
    batch: int = 64,
    aliases: dict[str, list[str]] | None = None,
    skip: Callable[[list[str]], set[str]] | None = None,
) -> Iterator[list[ArticleVector]]:
    """Percorre os artigos da fonte (só artigos: sem redirects nem recursos) e devolve os vetores
    em lotes de `batch`. `skip` recebe os `document_id` de um lote de caminhos e devolve os que
    não é preciso gerar (já têm vetor); `limit` conta os vetores gerados."""
    produced = 0
    paths: list[str] = []

    def work() -> list[ArticleVector]:
        nonlocal paths
        todo, paths = paths, []
        wanted = set(todo)
        if skip is not None:
            done = skip([make_document_id(source.id, p) for p in todo])
            wanted = {p for p in todo if make_document_id(source.id, p) not in done}
        articles: list[Article] = []
        for path in todo:
            if path not in wanted:
                continue
            try:
                articles.append(source.read(path))
            except SourceError:  # um artigo ilegível não pára a carga
                logger.warning("Vectors: %s/%s could not be read", source.id, path, exc_info=True)
        if limit is not None:
            articles = articles[: limit - produced]
        if not articles:
            return []
        vectors = embedder.embed_passages([passage_text(a) for a in articles])
        return [
            ArticleVector(
                source_id=source.id,
                path=a.path,
                title=a.title,
                language=source.language,
                version=source.version,
                model_id=embedder.model_id,
                embedding=vector,
                aliases=tuple(_own_aliases(a.title, (aliases or {}).get(a.path, ()))),
            )
            for a, vector in zip(articles, vectors, strict=True)
        ]

    for path in source.iter_paths():
        paths.append(path)
        if len(paths) >= batch:
            rows = work()
            if rows:
                produced += len(rows)
                yield rows
            if limit is not None and produced >= limit:
                return
    if paths:
        rows = work()
        if rows:
            yield rows


def _own_aliases(title: str, titles: Sequence[str]) -> list[str]:
    """Os títulos alternativos distintos do título (pela chave de comparação)."""
    seen = {title_key(title)}
    found = []
    for alias in titles:
        key = title_key(alias)
        if key and key not in seen:
            seen.add(key)
            found.append(alias)
    return found


# --- gravar na base de dados ---------------------------------------------------------------------


def store_vectors(session: Session, store: SemanticStore, rows: Sequence[ArticleVector]) -> int:
    """Upsert de `document` e `document_vector` (idempotente), mais os títulos alternativos.
    Não faz commit. Devolve quantos vetores foram gravados ou alterados. As fontes têm de existir
    em `source`; um documento que já existe não muda de versão."""
    if not rows:
        return 0
    by_document = {row.document_id: row for row in rows}
    values = [
        {
            "id": document_id,
            "source_id": row.source_id,
            "path": row.path,
            "version": row.version,
            "title": clean_text(row.title),
            "language": row.language,
            # Herda a classe da fonte (que tem de existir): um documento novo nunca fica sem ela.
            "license_class": select(SourceRow.license_class)
            .where(SourceRow.id == row.source_id)
            .scalar_subquery(),
        }
        for document_id, row in sorted(by_document.items())
    ]
    stmt = insert(DocumentRow).values(values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "title": stmt.excluded.title,
                "language": stmt.excluded.language,
                **relink_when(DocumentRow.title.is_distinct_from(stmt.excluded.title)),
            },
        )
    )
    add_aliases(session, {d: list(row.aliases) for d, row in by_document.items() if row.aliases})
    changed = 0
    for model_id in sorted({row.model_id for row in by_document.values()}):
        changed += store.put_article_vectors(
            session,
            model_id,
            {d: row.embedding for d, row in by_document.items() if row.model_id == model_id},
        )
    return changed


def missing_sources(session: Session, source_ids: Sequence[str]) -> list[str]:
    known = set(session.scalars(select(SourceRow.id).where(SourceRow.id.in_(list(source_ids)))))
    return sorted(set(source_ids) - known)


# --- Parquet -------------------------------------------------------------------------------------

COLUMNS = ("source_id", "path", "title", "language", "version", "model_id", "embedding")


def _pyarrow() -> tuple[Any, Any]:
    """O pyarrow só se carrega aqui: é uma biblioteca nativa que o servidor, a servir pedidos,
    não usa (e que tem de passar o canário do bench/embeddings no X5550)."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - dependência obrigatória
        raise VectorsError(f"pyarrow is missing to read/write Parquet: {exc}") from exc
    return pa, pq


class ParquetSink:
    """Escreve `ArticleVector` num ficheiro Parquet, em grupos de linhas de `ROW_GROUP` (grupos
    grandes comprimem melhor e deixam a leitura em lotes eficiente)."""

    def __init__(self, path: Path) -> None:
        pa, pq = _pyarrow()
        self._pa = pa
        self._schema = pa.schema(
            [
                ("source_id", pa.string()),
                ("path", pa.string()),
                ("title", pa.string()),
                ("language", pa.string()),
                ("version", pa.string()),
                ("model_id", pa.string()),
                ("embedding", pa.list_(pa.float32(), EMBEDDING_DIM)),
                ("aliases", pa.list_(pa.string())),
            ]
        )
        self._writer = pq.ParquetWriter(path, self._schema, compression="zstd")
        self._buffer: list[ArticleVector] = []

    def write(self, rows: Sequence[ArticleVector]) -> None:
        self._buffer.extend(rows)
        while len(self._buffer) >= ROW_GROUP:
            self._flush(ROW_GROUP)

    def _flush(self, count: int) -> None:
        pa = self._pa
        rows, self._buffer = self._buffer[:count], self._buffer[count:]
        if not rows:
            return
        table = pa.table(
            {
                "source_id": [r.source_id for r in rows],
                "path": [r.path for r in rows],
                "title": [r.title for r in rows],
                "language": [r.language for r in rows],
                "version": [r.version for r in rows],
                "model_id": [r.model_id for r in rows],
                "embedding": pa.array(
                    [r.embedding for r in rows], type=pa.list_(pa.float32(), EMBEDDING_DIM)
                ),
                "aliases": [list(r.aliases) for r in rows],
            },
            schema=self._schema,
        )
        self._writer.write_table(table)

    def close(self) -> None:
        self._flush(len(self._buffer))
        self._writer.close()

    def __enter__(self) -> "ParquetSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _check_model_statistics(parquet: Any, path: Path, expected_model_id: str) -> None:
    """Recusa já, sem ler a coluna, um ficheiro cujos row groups dizem (nas estatísticas) que
    têm outro modelo. Sem estatísticas, a verificação fica por lote."""
    meta = parquet.metadata
    for index in range(meta.num_row_groups):
        group = meta.row_group(index)
        for position in range(group.num_columns):
            column = group.column(position)
            stats = column.statistics
            if column.path_in_schema != "model_id" or stats is None or not stats.has_min_max:
                continue
            if stats.min != expected_model_id or stats.max != expected_model_id:
                raise VectorsError(
                    _wrong_model(path, f"{stats.min} … {stats.max}", expected_model_id)
                )


def _wrong_model(path: Path, found: str, expected: str) -> str:
    return (
        f"{path}: vectors of model {found}, but the current model is {expected}. Generate them "
        "again "
        "with the same model (or change the server EMBEDDER)."
    )


REQUIRED_TEXT = ("source_id", "path", "title", "version", "model_id")


def _bad_row(path: Path, number: int, data: dict[str, Any], i: int, why: str) -> VectorsError:
    where = f"{data['source_id'][i]}/{data['path'][i]}"
    return VectorsError(f"{path}: row {number} ({where}): {why}.")


def read_parquet(path: Path, *, expected_model_id: str) -> Iterator[list[ArticleVector]]:
    """Lê o ficheiro em lotes. Recusa (`VectorsError`, com a linha) um ficheiro sem as colunas
    esperadas, com outro `model_id` que não `expected_model_id`, com vetores de outra dimensão ou
    com NaN/infinitos, e linhas com campos nulos ou com NUL."""
    _, pq = _pyarrow()
    try:
        parquet = pq.ParquetFile(path)
    except (OSError, ValueError) as exc:
        raise VectorsError(f"{path}: not a readable Parquet file ({exc})") from exc
    names = set(parquet.schema_arrow.names)
    if absent := [c for c in COLUMNS if c not in names]:
        raise VectorsError(f"{path}: missing columns {', '.join(absent)}.")
    _check_model_statistics(parquet, path, expected_model_id)
    declared = getattr(parquet.schema_arrow.field("embedding").type, "list_size", EMBEDDING_DIM)
    if declared != EMBEDDING_DIM:
        raise VectorsError(
            f"{path}: the vectors have dimension {declared}, "
            f"and the tables are vector({EMBEDDING_DIM})."
        )
    wanted = [*COLUMNS, *(["aliases"] if "aliases" in names else [])]
    done = 0
    for batch in parquet.iter_batches(batch_size=PARQUET_BATCH, columns=wanted):
        data = batch.to_pydict()
        rows = []
        aliases = data.get("aliases") or [None] * len(data["path"])
        for i, embedding in enumerate(data["embedding"]):
            number = done + i + 1
            for column in REQUIRED_TEXT:
                value = data[column][i]
                if not isinstance(value, str) or not value:
                    raise _bad_row(path, number, data, i, f"{column} empty or null")
            texts = [data["title"][i], data["path"][i], data["language"][i], *(aliases[i] or ())]
            if any(isinstance(t, str) and "\x00" in t for t in texts):
                raise _bad_row(path, number, data, i, "text with NUL")
            if data["model_id"][i] != expected_model_id:
                raise VectorsError(_wrong_model(path, data["model_id"][i], expected_model_id))
            if embedding is None or len(embedding) != EMBEDDING_DIM:
                size = None if embedding is None else len(embedding)
                raise _bad_row(
                    path, number, data, i, f"vector with dimension {size}, expected {EMBEDDING_DIM}"
                )
            vector = np.asarray(embedding, dtype=np.float32)
            if not np.isfinite(vector).all():  # também apanha elementos nulos (NaN)
                raise _bad_row(path, number, data, i, "vector with NaN, infinity or nulls")
            rows.append(
                ArticleVector(
                    source_id=data["source_id"][i],
                    path=data["path"][i],
                    title=data["title"][i],
                    language=data["language"][i],
                    version=data["version"][i],
                    model_id=data["model_id"][i],
                    embedding=vector.tolist(),
                    aliases=tuple(aliases[i] or ()),
                )
            )
        done += len(rows)
        yield rows
