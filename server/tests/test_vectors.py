"""Vetores de artigo: o texto vetorizado, `vectors build --out`/`import` e a idempotência."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ariadne import queue
from ariadne.cli import app as cli
from ariadne.config import get_settings
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import DocumentVector, Job
from ariadne.documents import MAX_ALIASES
from ariadne.embedder import EMBEDDING_DIM, FakeEmbedder
from ariadne.services import Services
from ariadne.sources.registry import SourceRegistry
from ariadne.vectors import (
    ArticleVector,
    ParquetSink,
    VectorsError,
    collect_aliases,
    first_paragraph,
    iter_article_vectors,
    passage_text,
    read_parquet,
)
from ariadne.workers import indexer
from ariadne.workers.article_vector import KIND, enqueue_article_vector
from ariadne.workers.runner import Runner
from tests.fakes import default_fake
from tests.graphzims import (
    INTRO,
    REVOLUTION,
    WIKIBOOKS,
    WIKIPEDIA,
    make_graph_dir,
)
from tests.test_level3 import make_services

MODEL = "fake/embedder@0000000"


@pytest.fixture
def zims(tmp_path: Path) -> Path:
    return make_graph_dir(tmp_path / "graph")


@pytest.fixture
def registry(zims: Path) -> Iterator[SourceRegistry]:
    registry = SourceRegistry(zims)
    registry.load()
    yield registry
    registry.close()


# --- o texto ------------------------------------------------------------------------------------


def test_passagem_e_titulo_mais_primeiro_paragrafo(registry: SourceRegistry) -> None:
    source = registry.get(WIKIPEDIA)
    assert source is not None
    article = source.read(REVOLUTION)

    assert first_paragraph(article) == INTRO
    assert passage_text(article) == f"Revolução dos Cravos\n{INTRO}"


def test_passagem_longa_corta_em_palavra(registry: SourceRegistry) -> None:
    source = registry.get(WIKIPEDIA)
    assert source is not None
    article = source.read(REVOLUTION)
    from dataclasses import replace

    from ariadne.sources.base import Block

    long = replace(article, blocks=(Block("p", "palavra " * 400),))

    text = passage_text(long)

    assert len(text) <= len("Revolução dos Cravos\n") + 800
    assert text.endswith("palavra")


def test_aliases_sao_os_redirects_do_zim(registry: SourceRegistry) -> None:
    source = registry.get(WIKIPEDIA)
    assert source is not None

    assert collect_aliases(source) == {REVOLUTION: ["25 de Abril"]}


def test_iter_percorre_so_artigos_em_lotes_e_respeita_o_limite(registry: SourceRegistry) -> None:
    source = registry.get(WIKIPEDIA)
    assert source is not None
    embedder = FakeEmbedder()

    batches = list(iter_article_vectors(source, embedder, batch=2))
    limited = list(iter_article_vectors(source, embedder, batch=2, limit=2))

    assert [len(b) for b in batches] == [2, 1]  # 3 artigos; o redirect e o CSS ficam de fora
    rows = [row for batch in batches for row in batch]
    assert {r.path for r in rows} == {REVOLUTION, "Lisboa", "Cozinha"}
    assert all(r.model_id == MODEL and len(r.embedding) == EMBEDDING_DIM for r in rows)
    assert sum(len(b) for b in limited) == 2
    # o que se vetoriza é o título e o 1.º parágrafo (o prefixo `passage:` é do lado do modelo)
    kinds = {kind for kind, _ in embedder.calls}
    texts = [t for _, batch_texts in embedder.calls for t in batch_texts]
    assert kinds == {"passage"}
    assert f"Revolução dos Cravos\n{INTRO}" in texts


def test_iter_salta_o_que_ja_tem_vetor(registry: SourceRegistry) -> None:
    source = registry.get(WIKIPEDIA)
    assert source is not None
    embedder = FakeEmbedder()

    rows = [
        r
        for b in iter_article_vectors(
            source, embedder, skip=lambda ids: {i for i in ids if i.endswith("/Lisboa")}
        )
        for r in b
    ]

    assert {r.path for r in rows} == {REVOLUTION, "Cozinha"}


# --- Parquet: ida e volta -------------------------------------------------------------------


@pytest.fixture
def cold_env(monkeypatch: pytest.MonkeyPatch, database_url: str, zims: Path) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ZIM_DIR", str(zims))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def vectors_in_db(db: sessionmaker[Session]) -> dict[str, list[float]]:
    with db() as session:
        return {v.document_id: list(v.embedding) for v in session.scalars(select(DocumentVector))}


def test_build_out_nao_precisa_de_base_de_dados(
    monkeypatch: pytest.MonkeyPatch, zims: Path, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ZIM_DIR", str(zims))
    get_settings.cache_clear()
    out = tmp_path / "wikipedia.parquet"

    code, output = run("vectors", "build", "--source", WIKIPEDIA, "--out", str(out))

    get_settings.cache_clear()
    assert code == 0, output
    assert "3 vectors" in output
    rows = [r for batch in read_parquet(out, expected_model_id=MODEL) for r in batch]
    assert {r.path for r in rows} == {REVOLUTION, "Lisboa", "Cozinha"}
    by_path = {r.path: r for r in rows}
    assert by_path[REVOLUTION].aliases == ("25 de Abril",)
    assert (by_path[REVOLUTION].language, by_path[REVOLUTION].version) == ("pt", "2026-09")


@pytest.mark.db
def test_build_out_e_import_ida_e_volta_e_idempotente(
    cold_env: None, db: sessionmaker[Session], tmp_path: Path
) -> None:
    out = tmp_path / "wikibooks.parquet"
    assert run("vectors", "build", "--source", WIKIBOOKS, "--out", str(out))[0] == 0

    # Sem a fonte registada o import explica o que falta e não grava nada.
    code, output = run("vectors", "import", str(out))
    assert code == 1 and WIKIBOOKS in output and "sources sync" in output
    assert vectors_in_db(db) == {}

    assert run("sources", "sync")[0] == 0
    code, output = run("vectors", "import", str(out))

    assert code == 0, output
    assert "3 vectors read" in output and "3 saved or changed" in output
    got = vectors_in_db(db)
    assert set(got) == {f"{WIKIBOOKS}/{p}" for p in (REVOLUTION, "25_de_Abril", "Receitas")}
    with db() as session:
        titles = dict(session.execute(select(DocumentRow.id, DocumentRow.title)).all())
        assert titles[f"{WIKIBOOKS}/25_de_Abril"] == "25 de abril"
        assert session.scalar(select(func.count()).select_from(DocumentVector)) == 3
        assert (
            session.scalar(
                select(DocumentVector.model_id).where(
                    DocumentVector.document_id == f"{WIKIBOOKS}/Receitas"
                )
            )
            == MODEL
        )

    code, output = run("vectors", "import", str(out))  # repetir não muda nada
    assert code == 0 and "0 saved or changed" in output
    assert vectors_in_db(db) == got


@pytest.mark.db
def test_import_traz_os_titulos_alternativos_para_document_meta(
    cold_env: None, db: sessionmaker[Session], tmp_path: Path
) -> None:
    out = tmp_path / "wikipedia.parquet"
    assert run("vectors", "build", "--source", WIKIPEDIA, "--out", str(out))[0] == 0
    assert run("sources", "sync")[0] == 0

    assert run("vectors", "import", str(out))[0] == 0

    with db() as session:
        meta = session.scalar(
            select(DocumentRow.meta).where(DocumentRow.id == f"{WIKIPEDIA}/{REVOLUTION}")
        )
    assert meta == {"aliases": ["25 de Abril"]}


@pytest.mark.db
def test_build_direto_grava_e_so_gera_o_que_falta(
    cold_env: None, db: sessionmaker[Session]
) -> None:
    code, output = run("vectors", "build", "--source", WIKIPEDIA, "--limit", "2")
    assert code == 0, output
    assert len(vectors_in_db(db)) == 2

    code, output = run("vectors", "build", "--source", WIKIPEDIA)

    assert code == 0, output
    assert "1 vectors of" in output  # só o que faltava
    assert len(vectors_in_db(db)) == 3
    assert run("vectors", "build", "--source", "zim:nao_existe")[0] == 1


def write_parquet(path: Path, *, model_id: str = MODEL, dim: int = EMBEDDING_DIM) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "source_id": [WIKIPEDIA],
            "path": ["Lisboa"],
            "title": ["Lisboa"],
            "language": ["pt"],
            "version": ["2026-09"],
            "model_id": [model_id],
            "embedding": pa.array([[0.5] * dim], type=pa.list_(pa.float32())),
        }
    )
    pq.write_table(table, path)


@pytest.mark.db
def test_import_recusa_outro_modelo_ou_outra_dimensao(
    cold_env: None, db: sessionmaker[Session], tmp_path: Path
) -> None:
    assert run("sources", "sync")[0] == 0
    other_model = tmp_path / "outro-modelo.parquet"
    write_parquet(other_model, model_id="intfloat/multilingual-e5-small@614241f:int8")
    wrong_dim = tmp_path / "outra-dimensao.parquet"
    write_parquet(wrong_dim, dim=8)
    fixed_dim = tmp_path / "fixa.parquet"
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(
        pa.table(
            {
                "source_id": [WIKIPEDIA],
                "path": ["Lisboa"],
                "title": ["Lisboa"],
                "language": ["pt"],
                "version": ["2026-09"],
                "model_id": [MODEL],
                "embedding": pa.array([[0.5] * 8], type=pa.list_(pa.float32(), 8)),
            }
        ),
        fixed_dim,
    )
    no_columns = tmp_path / "sem-colunas.parquet"
    pq.write_table(pa.table({"path": ["x"]}), no_columns)

    code, output = run("vectors", "import", str(other_model))
    assert code == 1 and "modelo" in output and MODEL in output
    code, output = run("vectors", "import", str(wrong_dim))
    assert code == 1 and "dimension 8" in output
    code, output = run("vectors", "import", str(fixed_dim))
    assert code == 1 and "dimension 8" in output
    code, output = run("vectors", "import", str(no_columns))
    assert code == 1 and "missing columns" in output
    assert vectors_in_db(db) == {}


def test_parquet_sink_e_read_sao_inversos(tmp_path: Path) -> None:
    rows = [
        ArticleVector(WIKIPEDIA, "A", "A", "pt", "2026-09", MODEL, [0.25] * EMBEDDING_DIM, ("a1",)),
        ArticleVector(WIKIPEDIA, "B", "B", None, "2026-09", MODEL, [0.5] * EMBEDDING_DIM),
    ]
    with ParquetSink(tmp_path / "x.parquet") as sink:
        sink.write(rows)

    [back] = list(read_parquet(tmp_path / "x.parquet", expected_model_id=MODEL))

    assert back == rows
    with pytest.raises(VectorsError, match="vectors of model"):
        list(read_parquet(tmp_path / "x.parquet", expected_model_id="outro"))


# --- job article_vector ---------------------------------------------------------------------------


@pytest.fixture
def services(db: sessionmaker[Session], zims: Path) -> Iterator[Services]:
    services = make_services(db, zims, default_fake(), FakeEmbedder())
    yield services
    services.close()


@pytest.mark.db
def test_indexer_enfileira_o_vetor_de_artigo_e_o_job_grava_o_alias(
    services: Services, db: sessionmaker[Session]
) -> None:
    via_redirect = f"{WIKIPEDIA}/25_de_Abril"
    canonical = f"{WIKIPEDIA}/{REVOLUTION}"
    with db() as session:
        indexer.enqueue_index(session, via_redirect, 1.0)
        session.commit()
    runner = Runner(services, ["indexer"], worker_id="teste")

    assert runner.run_once()  # index_document

    with db() as session:
        kinds = {j.kind: j for j in session.scalars(select(Job)) if j.status == "pending"}
        assert kinds["article_vector"].payload == {"document_id": canonical}  # o canónico
        assert kinds["article_vector"].priority == 1.0
    assert runner.run_once()  # article_vector
    assert not runner.run_once()

    with db() as session:
        assert services.store.has_article_vectors(session, [canonical], MODEL) == {canonical}
        meta = session.scalar(select(DocumentRow.meta).where(DocumentRow.id == canonical))
        assert meta == {"indexed": {MODEL: "2026-09"}, "aliases": ["25 de Abril"]}
        # já tem vetor: um segundo documento indexado não o pede de novo
        assert not enqueue_article_vector(services, session, canonical, MODEL, 1.0)


@pytest.mark.db
def test_article_vector_adia_sem_embedder_e_falha_de_vez_sem_documento(
    services: Services, db: sessionmaker[Session]
) -> None:
    runner = Runner(services, ["indexer"], worker_id="teste")
    with db() as session:
        queue.enqueue(session, KIND, {"document_id": f"{WIKIPEDIA}/Lisboa"})
        queue.enqueue(session, KIND, {"document_id": f"{WIKIPEDIA}/Inexistente"}, priority=-1)
        session.commit()
    assert isinstance(services.embedder, FakeEmbedder)
    services.embedder.failing = True

    assert runner.run_once()  # Lisboa: embedder em baixo -> adiado, sem gastar a tentativa
    services.embedder.failing = False
    assert runner.run_once()  # Inexistente: falha de vez

    with db() as session:
        jobs = {j.payload["document_id"]: j for j in session.scalars(select(Job))}
        lisboa = jobs[f"{WIKIPEDIA}/Lisboa"]
        assert (lisboa.status, lisboa.attempts) == ("pending", 0)
        assert "embedder unavailable" in (lisboa.last_error or "")
        assert jobs[f"{WIKIPEDIA}/Inexistente"].status == "failed"


# --- validação do ficheiro e escrita em grupos grandes -----------------------------------------


def write_rows(target: Path, **overrides: object) -> None:
    """Um Parquet de 3 linhas válidas, com uma coluna/valor trocado por `overrides`."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    columns: dict[str, object] = {
        "source_id": [WIKIPEDIA] * 3,
        "path": ["A", "B", "C"],
        "title": ["A", "B", "C"],
        "language": ["pt"] * 3,
        "version": ["2026-09"] * 3,
        "model_id": [MODEL] * 3,
        "embedding": [[0.5] * EMBEDDING_DIM for _ in range(3)],
    }
    columns.update(overrides)
    schema = pa.schema(
        [
            ("source_id", pa.string()),
            ("path", pa.string()),
            ("title", pa.string()),
            ("language", pa.string()),
            ("version", pa.string()),
            ("model_id", pa.string()),
            ("embedding", pa.list_(pa.float32())),
        ]
    )
    pq.write_table(pa.table(columns, schema=schema), target)


def read_all(path: Path) -> list[ArticleVector]:
    return [row for batch in read_parquet(path, expected_model_id=MODEL) for row in batch]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"embedding": [[0.5] * EMBEDDING_DIM, [float("nan")] + [0.5] * 383, [0.5] * 384]}, "NaN"),
        ({"embedding": [[0.5] * 384, [0.5] * 384, [float("inf")] + [0.5] * 383]}, "infinity"),
        ({"embedding": [[0.5] * 384, [0.5] * 384, None]}, "dimension None"),
        ({"embedding": [[0.5] * 384, [0.5] * 384, [0.5] * 383]}, "dimension 383"),
        ({"title": ["A", None, "C"]}, "title empty or null"),
        ({"path": ["A", "B", ""]}, "path empty or null"),
        ({"title": ["A", "B\x00x", "C"]}, "NUL"),
        ({"model_id": [MODEL, MODEL, "outro@1"]}, "outro@1"),
    ],
)
def test_import_recusa_linhas_invalidas_com_a_linha_e_o_documento(
    tmp_path: Path, override: dict[str, object], message: str
) -> None:
    write_rows(tmp_path / "x.parquet", **override)

    with pytest.raises(VectorsError) as error:
        read_all(tmp_path / "x.parquet")

    text = str(error.value)
    assert message in text
    if "outro@1" not in message:  # o modelo errado é do ficheiro, não de uma linha
        assert "row " in text and f"{WIKIPEDIA}/" in text


def test_import_valida_o_modelo_pelas_estatisticas_sem_ler_a_coluna(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_rows(tmp_path / "x.parquet", model_id=["outro@1"] * 3)
    import pyarrow.parquet as pq

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("não devia ler a coluna model_id inteira")

    monkeypatch.setattr(pq.ParquetFile, "read", never)

    with pytest.raises(VectorsError, match="outro@1"):
        next(read_parquet(tmp_path / "x.parquet", expected_model_id=MODEL))

    write_rows(tmp_path / "ok.parquet")
    assert len(read_all(tmp_path / "ok.parquet")) == 3


def test_sink_agrega_grupos_de_linhas_grandes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyarrow.parquet as pq

    monkeypatch.setattr("ariadne.vectors.ROW_GROUP", 100)
    rows = [
        ArticleVector(WIKIPEDIA, f"P{n}", f"P{n}", "pt", "2026-09", MODEL, [0.1] * EMBEDDING_DIM)
        for n in range(250)
    ]
    with ParquetSink(tmp_path / "x.parquet") as sink:
        for start in range(0, 250, 64):  # lotes pequenos, como o `build`
            sink.write(rows[start : start + 64])

    meta = pq.ParquetFile(tmp_path / "x.parquet").metadata
    assert [meta.row_group(i).num_rows for i in range(meta.num_row_groups)] == [100, 100, 50]
    assert [r.path for r in read_all(tmp_path / "x.parquet")] == [r.path for r in rows]


def test_collect_aliases_poe_o_tecto_ja_na_recolha(registry: SourceRegistry) -> None:
    source = registry.get(WIKIPEDIA)
    assert source is not None
    many = [(f"Titulo alternativo {n}", REVOLUTION) for n in range(120)]
    source.redirects = lambda: iter([*many, *many[:5]])  # type: ignore[method-assign]

    aliases = collect_aliases(source)

    assert len(aliases[REVOLUTION]) == MAX_ALIASES == 50
    assert len(set(aliases[REVOLUTION])) == 50
