"""Fase 6: estado da curadoria por documento (fila de revisão humana, publicação no Wiki.js)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "curation",
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("sampled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("curator_model", sa.Text(), nullable=True),
        sa.Column("fidelity", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("original_md", sa.Text(), nullable=False),
        sa.Column("curated_md", sa.Text(), nullable=True),
        sa.Column("published_md", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('published', 'pending_curator', 'needs_review', 'rejected')",
            name=op.f("ck_curation_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name=op.f("fk_curation_document_id_document")
        ),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_curation")),
    )
    op.create_index(
        "ix_curation_decision_updated",
        "curation",
        ["decision", sa.text("updated_at DESC"), "document_id"],
    )

    # O caminho de uma página (`document.meta.wiki_path`) reserva-se antes de escrever e é único.
    op.create_index(
        "ix_document_wiki_path",
        "document",
        [sa.text("(meta ->> 'wiki_path')")],
        unique=True,
        postgresql_where=sa.text("(meta ->> 'wiki_path') IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_wiki_path",
        table_name="document",
        postgresql_where=sa.text("(meta ->> 'wiki_path') IS NOT NULL"),
    )
    op.drop_index("ix_curation_decision_updated", table_name="curation")
    op.drop_table("curation")
