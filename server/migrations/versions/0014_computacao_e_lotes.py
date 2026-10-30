"""Controlo da curadoria e destinos de computação: `compute_control`, `batch_run`, `batch_task`.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-06 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
ZERO = sa.text("0")


def upgrade() -> None:
    op.create_table(
        "compute_control",
        sa.Column("id", sa.SmallInteger(), primary_key=True, server_default=sa.text("1")),
        sa.Column("state", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("destination", sa.Text(), server_default=sa.text("'local'"), nullable=False),
        sa.Column("fallback_local", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "schedule_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("window_start", sa.Text(), server_default=sa.text("'23:00'"), nullable=False),
        sa.Column("window_end", sa.Text(), server_default=sa.text("'07:00'"), nullable=False),
        sa.Column("timezone", sa.Text(), server_default=sa.text("'UTC'"), nullable=False),
        sa.Column("manual_override", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "disabled_kinds",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("batch_size", sa.Integer(), server_default=sa.text("200"), nullable=False),
        sa.Column("max_run_hours", sa.Float(), server_default=sa.text("8"), nullable=False),
        sa.Column(
            "destinations",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("external_blocked_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_compute_control_single_row")),
        sa.CheckConstraint(
            "state IN ('stopped', 'running', 'paused')", name=op.f("ck_compute_control_state")
        ),
    )
    op.execute("INSERT INTO compute_control (id) VALUES (1)")

    op.create_table(
        "batch_run",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'created'"), nullable=False),
        sa.Column("manual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "kinds", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=True),
        sa.Column("task_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("request_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("applied_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("review_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("invalid_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("released_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column(
            "external_ref",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("deadline_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("running_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gpu_seconds", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # O JSONL de pedidos e o de resultados, comprimidos (apagam-se quando a corrida acaba).
        sa.Column("tasks_gz", sa.LargeBinary(), nullable=True),
        sa.Column("results_gz", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint(
            "status IN ('created', 'queued', 'running', 'awaiting_results', 'importing', "
            "'done', 'failed', 'cancelled')",
            name=op.f("ck_batch_run_status"),
        ),
    )
    op.create_index("ix_batch_run_created", "batch_run", [sa.text("created_at DESC")])
    # No máximo uma corrida em curso: a quota de GPU é uma só.
    op.create_index(
        "uq_batch_run_in_progress",
        "batch_run",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('created', 'queued', 'running', 'awaiting_results', 'importing')"
        ),
    )

    op.create_table(
        "batch_task",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("batch_run.id", name=op.f("fk_batch_task_run_id_batch_run")),
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "meta", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "request_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        # exported | applied | review | invalid | missing
        sa.Column("status", sa.Text(), server_default=sa.text("'exported'"), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )
    op.create_index("ix_batch_task_run", "batch_task", ["run_id"])
    op.create_index("ix_batch_task_job", "batch_task", ["job_id"])

    op.add_column(
        "curation",
        sa.Column(
            "batch_run_id",
            sa.BigInteger(),
            sa.ForeignKey("batch_run.id", name=op.f("fk_curation_batch_run_id_batch_run")),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_curation_batch_run",
        "curation",
        ["batch_run_id", "decision"],
        postgresql_where=sa.text("batch_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_curation_batch_run", table_name="curation")
    op.drop_column("curation", "batch_run_id")
    op.drop_index("ix_batch_task_job", table_name="batch_task")
    op.drop_index("ix_batch_task_run", table_name="batch_task")
    op.drop_table("batch_task")
    op.drop_index("uq_batch_run_in_progress", table_name="batch_run")
    op.drop_index("ix_batch_run_created", table_name="batch_run")
    op.drop_table("batch_run")
    op.drop_table("compute_control")
