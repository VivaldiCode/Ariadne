"""Cofre de segredos e configuração das integrações: `integration_setting`, `integration_audit`.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-08 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "integration_setting",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("integration", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("is_secret", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
    )
    op.create_table(
        "integration_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=True),
        sa.Column("integration", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_integration_audit_at", "integration_audit", ["at"])


def downgrade() -> None:
    op.drop_index("ix_integration_audit_at", table_name="integration_audit")
    op.drop_table("integration_audit")
    op.drop_table("integration_setting")
