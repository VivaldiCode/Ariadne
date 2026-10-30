"""Chaves de clientes: suspensão reversível, quota mensal de pedidos e criação idempotente
(`external_id`); mais o índice que o uso por chave e a quota consultam.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-30 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("principal", sa.Column("suspended_at", sa.DateTime(timezone=True)))
    op.add_column("principal", sa.Column("external_id", sa.Text()))
    op.add_column("principal", sa.Column("monthly_query_quota", sa.Integer()))
    op.create_unique_constraint(op.f("uq_principal_external_id"), "principal", ["external_id"])
    # Suspensão e quota são dos clientes externos (chaves `mcp`); as chaves do dono nunca se
    # suspendem (não ficavam sem acesso ao próprio servidor) nem têm quota.
    op.create_check_constraint(
        op.f("ck_principal_suspended_mcp"),
        "principal",
        "suspended_at IS NULL OR scope = 'mcp'",
    )
    op.create_check_constraint(
        op.f("ck_principal_quota_mcp"),
        "principal",
        "monthly_query_quota IS NULL OR (scope = 'mcp' AND monthly_query_quota > 0)",
    )
    op.create_index(
        "ix_tool_call_principal_id_created_at", "tool_call", ["principal_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tool_call_principal_id_created_at", table_name="tool_call")
    op.drop_constraint(op.f("ck_principal_quota_mcp"), "principal", type_="check")
    op.drop_constraint(op.f("ck_principal_suspended_mcp"), "principal", type_="check")
    op.drop_constraint(op.f("uq_principal_external_id"), "principal", type_="unique")
    op.drop_column("principal", "monthly_query_quota")
    op.drop_column("principal", "external_id")
    op.drop_column("principal", "suspended_at")
