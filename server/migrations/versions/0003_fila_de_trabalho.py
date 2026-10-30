"""fila de trabalho (bloqueio, erro, fim, chave de deduplicação) e índice do orçamento diário

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job", sa.Column("locked_by", sa.Text(), nullable=True))
    op.add_column("job", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("job", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job", sa.Column("dedupe_key", sa.Text(), nullable=True))
    op.create_index(
        "ix_job_pending_priority_run_after",
        "job",
        [sa.literal_column("priority DESC"), "run_after"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_job_pending_dedupe_key",
        "job",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_index(
        "ix_chunk_vector_model_id_created_at", "chunk_vector", ["model_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_chunk_vector_model_id_created_at", table_name="chunk_vector")
    op.drop_index(
        "uq_job_pending_dedupe_key",
        table_name="job",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        "ix_job_pending_priority_run_after",
        table_name="job",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_column("job", "dedupe_key")
    op.drop_column("job", "finished_at")
    op.drop_column("job", "last_error")
    op.drop_column("job", "locked_by")
    op.drop_column("job", "locked_at")
