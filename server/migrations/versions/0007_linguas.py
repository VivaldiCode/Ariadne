"""Fase 7: línguas — origem da língua original, cache de traduções, marcas nos resultados

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Como se soube a língua original do conceito: 'manual' (curadoria) ou 'country:XX' (TSV).
    op.add_column("concept", sa.Column("origin_source", sa.Text(), nullable=True))
    # Os documentos de um conceito (`translations`, regra histórica).
    op.create_index("ix_document_concept_id", "document", ["concept_id"])

    # A versão na língua original e o resultado que só a pergunta traduzida encontrou.
    for column in ("original_source", "translated_query"):
        op.add_column(
            "query_result",
            sa.Column(column, sa.Boolean(), server_default=sa.text("false"), nullable=False),
        )

    # Cache das traduções (chave = hash de língua origem, destino, termos protegidos e texto).
    op.create_table(
        "translation_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("source_lang", sa.Text(), nullable=False),
        sa.Column("target_lang", sa.Text(), nullable=False),
        sa.Column("translated", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_translation_cache")),
    )


def downgrade() -> None:
    op.drop_table("translation_cache")
    op.drop_column("query_result", "translated_query")
    op.drop_column("query_result", "original_source")
    op.drop_index("ix_document_concept_id", table_name="document")
    op.drop_column("concept", "origin_source")
