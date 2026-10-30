"""Importação v2 para o Wiki.js: a árvore de domínios e a colocação dos documentos

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-12 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "domain",
        sa.Column("id", sa.Text(), primary_key=True),  # 'saude', 'saude/medicamentos'
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("names", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("slugs", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("descriptions", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["domain.id"], name=op.f("fk_domain_parent_id_domain")
        ),
    )
    op.create_index("ix_domain_parent_id", "domain", ["parent_id"])

    op.create_table(
        "domain_vector",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),  # '<repo>@<revisão>'
        sa.Column("embedding", Vector(384), nullable=False),
        sa.PrimaryKeyConstraint("domain_id", "model_id", name=op.f("pk_domain_vector")),
        sa.ForeignKeyConstraint(
            ["domain_id"], ["domain.id"], name=op.f("fk_domain_vector_domain_id_domain")
        ),
    )

    op.create_table(
        "domain_assignment",
        sa.Column("document_id", sa.Text(), primary_key=True),
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("signals", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("needs_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name=op.f("fk_domain_assignment_document_id_document")
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"], ["domain.id"], name=op.f("fk_domain_assignment_domain_id_domain")
        ),
        sa.CheckConstraint(
            "method IN ('prior', 'vector', 'structure', 'llm', 'human', 'fallback')",
            name=op.f("ck_domain_assignment_method"),
        ),
    )
    op.create_index("ix_domain_assignment_domain_id", "domain_assignment", ["domain_id"])
    op.create_index(
        "ix_domain_assignment_review",
        "domain_assignment",
        ["document_id"],
        postgresql_where=sa.text("needs_review"),
    )


def downgrade() -> None:
    op.drop_index("ix_domain_assignment_review", table_name="domain_assignment")
    op.drop_index("ix_domain_assignment_domain_id", table_name="domain_assignment")
    op.drop_table("domain_assignment")
    op.drop_table("domain_vector")
    op.drop_index("ix_domain_parent_id", table_name="domain")
    op.drop_table("domain")
