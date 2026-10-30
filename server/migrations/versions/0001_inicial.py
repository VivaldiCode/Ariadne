"""inicial: tabelas da secção 10 do ARCHITECTURE.md

Revision ID: 0001
Revises:
Create Date: 2026-10-24 10:24:13.527762
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "concept",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("wikidata_qid", sa.Text(), nullable=True),
        sa.Column("origin_lang", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_concept")),
        sa.UniqueConstraint("wikidata_qid", name=op.f("uq_concept_wikidata_qid")),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priority", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
    )
    op.create_index(
        "ix_job_status_priority_run_after",
        "job",
        ["status", sa.literal_column("priority DESC"), "run_after"],
        unique=False,
    )
    op.create_table(
        "principal",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("api_key_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_principal")),
        sa.UniqueConstraint("api_key_hash", name=op.f("uq_principal_api_key_hash")),
    )
    op.create_table(
        "source",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("wing", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source")),
    )
    op.create_table(
        "document",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("concept_id", sa.BigInteger(), nullable=True),
        sa.Column("room", sa.Text(), nullable=True),
        sa.Column("wiki_page_id", sa.Integer(), nullable=True),
        sa.Column("heat", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "heat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["concept_id"], ["concept.id"], name=op.f("fk_document_concept_id_concept")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name=op.f("fk_document_source_id_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document")),
    )
    op.create_table(
        "query_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("principal_id", sa.BigInteger(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("query_lang", sa.Text(), nullable=True),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("level_served", sa.SmallInteger(), nullable=True),
        sa.Column("top_score", sa.REAL(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principal.id"], name=op.f("fk_query_log_principal_id_principal")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_query_log")),
    )
    op.create_table(
        "chunk",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("heat", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "heat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name=op.f("fk_chunk_document_id_document")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunk")),
        sa.UniqueConstraint("document_id", "ord", name=op.f("uq_chunk_document_id_ord")),
    )
    op.create_table(
        "document_vector",
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name=op.f("fk_document_vector_document_id_document")
        ),
        sa.PrimaryKeyConstraint("document_id", "model_id", name=op.f("pk_document_vector")),
    )
    op.create_table(
        "link",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("src_document", sa.Text(), nullable=False),
        sa.Column("dst_document", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("score", sa.REAL(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'accepted'"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dst_document"], ["document.id"], name=op.f("fk_link_dst_document_document")
        ),
        sa.ForeignKeyConstraint(
            ["src_document"], ["document.id"], name=op.f("fk_link_src_document_document")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_link")),
        sa.UniqueConstraint(
            "src_document",
            "dst_document",
            "kind",
            name=op.f("uq_link_src_document_dst_document_kind"),
        ),
    )
    op.create_table(
        "chunk_vector",
        sa.Column("chunk_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["chunk.id"], name=op.f("fk_chunk_vector_chunk_id_chunk")
        ),
        sa.PrimaryKeyConstraint("chunk_id", "model_id", name=op.f("pk_chunk_vector")),
    )
    op.create_index(
        "ix_chunk_vector_embedding_hnsw",
        "chunk_vector",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "query_result",
        sa.Column("query_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=True),
        sa.Column("chunk_id", sa.BigInteger(), nullable=True),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("score", sa.REAL(), nullable=True),
        sa.Column("used", sa.Boolean(), nullable=True),
        sa.Column("feedback", sa.SmallInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["chunk.id"], name=op.f("fk_query_result_chunk_id_chunk")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name=op.f("fk_query_result_document_id_document")
        ),
        sa.ForeignKeyConstraint(
            ["query_id"], ["query_log.id"], name=op.f("fk_query_result_query_id_query_log")
        ),
        sa.PrimaryKeyConstraint("query_id", "rank", name=op.f("pk_query_result")),
    )


def downgrade() -> None:
    # A extensão vector fica: pode ser usada por outras bases/esquemas.
    op.drop_table("query_result")
    op.drop_index(
        "ix_chunk_vector_embedding_hnsw",
        table_name="chunk_vector",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("chunk_vector")
    op.drop_table("link")
    op.drop_table("document_vector")
    op.drop_table("chunk")
    op.drop_table("query_log")
    op.drop_table("document")
    op.drop_table("source")
    op.drop_table("principal")
    op.drop_index("ix_job_status_priority_run_after", table_name="job")
    op.drop_table("job")
    op.drop_table("concept")
