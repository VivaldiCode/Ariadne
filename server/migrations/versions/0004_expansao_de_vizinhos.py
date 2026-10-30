"""document.expanded_at: quando o worker `neighbors` expandiu a vizinhança do documento

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document", sa.Column("expanded_at", sa.DateTime(timezone=True), nullable=True))
    # `recently_failed` procura por `dedupe_key` entre os jobs falhados.
    op.create_index(
        "ix_job_failed_dedupe_key",
        "job",
        ["dedupe_key"],
        unique=False,
        postgresql_where=sa.text("status = 'failed'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_job_failed_dedupe_key", table_name="job", postgresql_where=sa.text("status = 'failed'")
    )
    op.drop_column("document", "expanded_at")
