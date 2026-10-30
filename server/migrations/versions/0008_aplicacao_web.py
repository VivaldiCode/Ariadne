"""Fase 8: aplicação web — papéis e revogação de chaves nos principais, coleções a pré-aquecer

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "principal",
        sa.Column("role", sa.Text(), server_default=sa.text("'member'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_principal_role"), "principal", "role IN ('admin', 'member')"
    )
    op.add_column("principal", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    # Até aqui um `user` podia tudo: os que já existem ficam administradores.
    op.execute("UPDATE principal SET role = 'admin' WHERE kind = 'user'")

    op.create_table(
        "collection",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("entries", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("priority", sa.REAL(), nullable=False),
        sa.Column("queued", sa.Integer(), nullable=False),
        sa.Column("known", sa.Integer(), nullable=False),
        sa.Column("ignored", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name=op.f("fk_collection_source_id_source")
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principal.id"], name=op.f("fk_collection_principal_id_principal")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection")),
    )
    op.create_index(
        "ix_collection_principal_id_created_at", "collection", ["principal_id", "created_at"]
    )

    # As estatísticas agregam por intervalo de datas.
    op.create_index("ix_query_log_created_at", "query_log", ["created_at"])
    op.create_index("ix_tool_call_created_at", "tool_call", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_tool_call_created_at", table_name="tool_call")
    op.drop_index("ix_query_log_created_at", table_name="query_log")
    op.drop_table("collection")
    op.drop_column("principal", "revoked_at")
    op.drop_constraint(op.f("ck_principal_role"), "principal", type_="check")
    op.drop_column("principal", "role")
