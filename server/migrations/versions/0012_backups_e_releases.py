"""Fase 12: registo de backups e releases (`backup_run`)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-05 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "backup_run",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        # `backup` = pg_dump local (tudo, fica no NAS); `release` = a release externa
        # (só a classe aberto, encriptada com age, em partes).
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),  # a pasta (backup-… / ariadne-release-…)
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("parts", sa.Integer(), nullable=True),
        sa.Column("content_sha256", sa.Text(), nullable=True),
        sa.Column("manifest_sha256", sa.Text(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        # Publicação (só releases): quando e onde.
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_url", sa.Text(), nullable=True),
        # O ensaio de restauro (só releases): feito por quem tem a chave privada, fora do sistema.
        sa.Column("rehearsal_status", sa.Text(), nullable=True),
        sa.Column("rehearsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rehearsal_details", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("kind IN ('backup', 'release')", name=op.f("ck_backup_run_kind")),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'failed')", name=op.f("ck_backup_run_status")
        ),
        sa.CheckConstraint(
            "rehearsal_status IS NULL OR rehearsal_status IN ('ok', 'failed')",
            name=op.f("ck_backup_run_rehearsal_status"),
        ),
    )
    op.create_index(
        "ix_backup_run_kind_started", "backup_run", ["kind", sa.text("started_at DESC")]
    )


def downgrade() -> None:
    op.drop_index("ix_backup_run_kind_started", table_name="backup_run")
    op.drop_table("backup_run")
