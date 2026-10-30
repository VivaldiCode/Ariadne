"""Fase 9: salas MemPalace — tabela `room` e `document.room` a referenciá-la

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "room",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("wing", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("centroid", Vector(384), nullable=False),
        sa.Column("size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("base_size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("ready", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_room")),
    )
    op.create_index("ix_room_wing", "room", ["wing"])
    # Até aqui `document.room` era um texto que nada preenchia: passa a ser o id da sala.
    op.drop_column("document", "room")
    op.add_column("document", sa.Column("room", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_document_room_room"), "document", "room", ["room"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_document_room", "document", ["room"])


def downgrade() -> None:
    op.drop_index("ix_document_room", table_name="document")
    op.drop_constraint(op.f("fk_document_room_room"), "document", type_="foreignkey")
    op.drop_column("document", "room")
    op.add_column("document", sa.Column("room", sa.Text(), nullable=True))
    op.drop_index("ix_room_wing", table_name="room")
    op.drop_table("room")
