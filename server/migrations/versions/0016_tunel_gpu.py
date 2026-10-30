"""O túnel do lote de GPU: o token de uma corrida e o progresso que o notebook comunica.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-07 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Só o SHA-256 do token (um por corrida); o token nunca fica guardado.
    op.add_column("batch_run", sa.Column("token_hash", sa.Text(), nullable=True))
    op.add_column(
        "batch_run", sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "batch_run", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    # {"done": n, "total": n, "gpu_seconds": s}: o que o notebook diz que já fez.
    op.add_column(
        "batch_run",
        sa.Column(
            "progress",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("batch_run", "progress")
    op.drop_column("batch_run", "last_heartbeat_at")
    op.drop_column("batch_run", "token_expires_at")
    op.drop_column("batch_run", "token_hash")
