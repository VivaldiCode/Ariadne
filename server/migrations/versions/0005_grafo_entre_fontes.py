"""Fase 5: índice HNSW dos vetores de artigo, chaves de título e estado do linker, curadoria

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Secção 10, notas: pesquisa aproximada de títulos (hoje o `title_match` é por igualdade
    # normalizada; a extensão fica pronta para as propostas por semelhança de título).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.add_column(
        "document",
        sa.Column(
            "title_keys",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column("document", sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True))
    # Concessão do linker (um documento só fica visto no fim) e tentativas (isola os que falham).
    op.add_column("document", sa.Column("link_lease", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "document",
        sa.Column("link_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index("ix_document_title_keys", "document", ["title_keys"], postgresql_using="gin")
    op.create_index(
        "ix_document_unlinked",
        "document",
        ["id"],
        postgresql_where=sa.text("linked_at IS NULL"),
    )

    op.create_index(
        "ix_document_vector_embedding_hnsw",
        "document_vector",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.add_column("link", sa.Column("reviewed_by", sa.Text(), nullable=True))
    op.add_column("link", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_link_dst_document", "link", ["dst_document"])
    # Fila de curadoria: pela pontuação e por id (paginação por cursor, sem ordenar).
    op.create_index(
        "ix_link_proposed",
        "link",
        [sa.text("score DESC NULLS LAST"), "id"],
        postgresql_where=sa.text("status = 'proposed'"),
    )
    # Só `zim_internal` tem sentido; as outras arestas são não dirigidas, guardadas com src < dst.
    op.create_check_constraint(
        op.f("ck_link_undirected_order"),
        "link",
        'kind = \'zim_internal\' OR src_document COLLATE "C" < dst_document COLLATE "C"',
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_link_undirected_order"), "link", type_="check")
    op.drop_index(
        "ix_link_proposed", table_name="link", postgresql_where=sa.text("status = 'proposed'")
    )
    op.drop_index("ix_link_dst_document", table_name="link")
    op.drop_column("link", "reviewed_at")
    op.drop_column("link", "reviewed_by")
    op.drop_index(
        "ix_document_vector_embedding_hnsw",
        table_name="document_vector",
        postgresql_using="hnsw",
    )
    op.drop_index(
        "ix_document_unlinked", table_name="document", postgresql_where=sa.text("linked_at IS NULL")
    )
    op.drop_index("ix_document_title_keys", table_name="document", postgresql_using="gin")
    op.drop_column("document", "link_attempts")
    op.drop_column("document", "link_lease")
    op.drop_column("document", "linked_at")
    op.drop_column("document", "title_keys")
    # A extensão pg_trgm fica (pode ser usada por outra coisa).
