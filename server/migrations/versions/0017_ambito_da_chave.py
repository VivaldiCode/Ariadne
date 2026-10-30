"""O âmbito de cada chave: `full` (a de sempre) ou `mcp` (clientes externos: só o MCP e só o
conteúdo aberto).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-29 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "principal",
        sa.Column("scope", sa.Text(), server_default=sa.text("'full'"), nullable=False),
    )
    op.create_check_constraint(op.f("ck_principal_scope"), "principal", "scope IN ('full', 'mcp')")
    # Uma chave `mcp` é sempre de um agente sem privilégios: o Postgres garante-o.
    op.create_check_constraint(
        op.f("ck_principal_scope_mcp_member"),
        "principal",
        "scope = 'full' OR (kind = 'agent' AND role = 'member')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_principal_scope_mcp_member"), "principal", type_="check")
    op.drop_constraint(op.f("ck_principal_scope"), "principal", type_="check")
    op.drop_column("principal", "scope")
