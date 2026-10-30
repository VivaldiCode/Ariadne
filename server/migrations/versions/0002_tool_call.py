"""tool_call: registo das chamadas às ferramentas MCP e aos endpoints REST

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24 12:20:10.267096
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_call",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("principal_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("query_id", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principal.id"], name=op.f("fk_tool_call_principal_id_principal")
        ),
        sa.ForeignKeyConstraint(
            ["query_id"], ["query_log.id"], name=op.f("fk_tool_call_query_id_query_log")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_call")),
    )
    op.create_index(op.f("ix_tool_call_session_id"), "tool_call", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tool_call_session_id"), table_name="tool_call")
    op.drop_table("tool_call")
