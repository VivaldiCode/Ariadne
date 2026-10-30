import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from ariadne.db.migrate import downgrade, upgrade
from ariadne.db.models import Base

pytestmark = pytest.mark.db

TABLES = {
    "source",
    "concept",
    "document",
    "document_vector",
    "chunk",
    "chunk_vector",
    "link",
    "principal",
    "query_log",
    "query_result",
    "job",
    "tool_call",
    "curation",
    "translation_cache",
    "collection",
    "room",
    "course",
    "course_version",
    "lesson",
    "exercise",
    "book",
    "chapter",
    "license_block",
    "backup_run",
    "compute_control",
    "batch_run",
    "batch_task",
    "domain",
    "domain_vector",
    "domain_assignment",
    "integration_setting",
    "integration_audit",
}


def test_upgrade_cria_o_esquema_da_seccao_10(empty_database_url: str) -> None:
    upgrade(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        assert set(inspect(engine).get_table_names()) == TABLES | {"alembic_version"}
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
            hnsw = conn.scalar(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": "ix_chunk_vector_embedding_hnsw"},
            )
            assert hnsw is not None
            assert "USING hnsw" in hnsw
            assert "vector_cosine_ops" in hnsw
    finally:
        engine.dispose()


def test_modelos_e_migracao_coincidem(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()
    assert diff == []


def test_downgrade_e_upgrade_de_novo(empty_database_url: str) -> None:
    upgrade(empty_database_url)
    downgrade(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        upgrade(empty_database_url)
        assert set(inspect(engine).get_table_names()) == TABLES | {"alembic_version"}
    finally:
        engine.dispose()


def test_pesquisa_por_cosseno(empty_database_url: str) -> None:
    """Confirma que o pgvector do contentor calcula distâncias (útil no X5550, sem AVX)."""
    upgrade(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO source (id, kind, title, version, wing, license_class)"
                    " VALUES ('zim:teste', 'zim', 'Teste', '2026-10', 'teste', 'open')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO document (id, source_id, path, version, title, license_class)"
                    " VALUES ('zim:teste/A', 'zim:teste', 'A', '2026-10', 'A', 'open')"
                )
            )
            for ord_, axis in enumerate((0, 1, 2)):
                vec = [0.0] * 384
                vec[axis] = 1.0
                chunk_id = conn.scalar(
                    text(
                        "INSERT INTO chunk (document_id, ord, text)"
                        " VALUES ('zim:teste/A', :ord, 'x') RETURNING id"
                    ),
                    {"ord": ord_},
                )
                conn.execute(
                    text(
                        "INSERT INTO chunk_vector (chunk_id, model_id, embedding)"
                        " VALUES (:id, 'teste@0', :emb)"
                    ),
                    {"id": chunk_id, "emb": str(vec)},
                )
            query = [0.0] * 384
            query[1] = 1.0
            nearest = conn.execute(
                text(
                    "SELECT c.ord, v.embedding <=> CAST(:q AS vector) AS dist"
                    " FROM chunk_vector v JOIN chunk c ON c.id = v.chunk_id"
                    " ORDER BY dist LIMIT 1"
                ),
                {"q": str(query)},
            ).one()
    finally:
        engine.dispose()
    assert nearest.ord == 1
    assert nearest.dist == pytest.approx(0.0)


def test_fila_tem_colunas_e_indices_da_migracao_0003(empty_database_url: str) -> None:
    upgrade(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("job")}
        assert {"locked_at", "locked_by", "last_error", "finished_at", "dedupe_key"} <= columns
        with engine.connect() as conn:
            defs = dict(
                conn.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'job'")
                ).all()
            )
        assert "UNIQUE" in defs["uq_job_pending_dedupe_key"]
        assert "WHERE (status = 'pending'::text)" in defs["uq_job_pending_dedupe_key"]
        assert "priority DESC, run_after" in defs["ix_job_pending_priority_run_after"]
        with engine.connect() as conn:
            budget = conn.scalar(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                {"n": "ix_chunk_vector_model_id_created_at"},
            )
        assert budget is not None and "(model_id, created_at)" in budget
        assert "WHERE (status = 'pending'::text)" in defs["ix_job_pending_priority_run_after"]
    finally:
        engine.dispose()


def test_migracao_0004_acrescenta_e_remove_expanded_at(empty_database_url: str) -> None:
    upgrade(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        assert "expanded_at" in {c["name"] for c in inspect(engine).get_columns("document")}
        with engine.connect() as conn:
            failed = conn.scalar(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_job_failed_dedupe_key'")
            )
        assert failed is not None and "WHERE (status = 'failed'::text)" in failed
        downgrade(empty_database_url, "0003")
        assert "expanded_at" not in {c["name"] for c in inspect(engine).get_columns("document")}
    finally:
        engine.dispose()


def test_migracao_0005_cria_hnsw_chaves_de_titulo_e_curadoria(empty_database_url: str) -> None:
    upgrade(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        with engine.connect() as conn:
            defs = dict(
                conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes"
                        " WHERE tablename IN ('document', 'document_vector', 'link')"
                    )
                ).all()
            )
            assert conn.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"))
        hnsw = defs["ix_document_vector_embedding_hnsw"]
        assert "USING hnsw" in hnsw and "vector_cosine_ops" in hnsw
        assert "USING gin (title_keys)" in defs["ix_document_title_keys"]
        assert "WHERE (linked_at IS NULL)" in defs["ix_document_unlinked"]
        assert "WHERE (status = 'proposed'::text)" in defs["ix_link_proposed"]
        assert "score DESC NULLS LAST, id" in defs["ix_link_proposed"]
        document_columns = {c["name"] for c in inspect(engine).get_columns("document")}
        assert {"link_lease", "link_attempts"} <= document_columns
        with engine.connect() as conn:
            check = conn.scalar(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
                {"n": "ck_link_undirected_order"},
            )
        assert check is not None and "zim_internal" in check
        assert {"title_keys", "linked_at"} <= {
            c["name"] for c in inspect(engine).get_columns("document")
        }
        assert {"reviewed_by", "reviewed_at"} <= {
            c["name"] for c in inspect(engine).get_columns("link")
        }
        downgrade(empty_database_url, "0004")
        assert "title_keys" not in {c["name"] for c in inspect(engine).get_columns("document")}
        assert "reviewed_by" not in {c["name"] for c in inspect(engine).get_columns("link")}
    finally:
        engine.dispose()
