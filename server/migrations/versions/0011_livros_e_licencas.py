"""Fase 11: classes de licença (aberto | privado) e base de livros (`book`, `chapter`)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
ZERO = sa.text("0")
LICENSE_CHECK = "license_class IN ('open', 'private')"


def upgrade() -> None:
    # `license_class` (STRUCTURE.md, secção 7): NOT NULL e **sem valor por omissão** no fim desta
    # migração, para que quem cria uma fonte ou um documento tenha de a dizer. O que já existe
    # (ZIM e cursos) preenche-se aqui: os ZIM são abertos; os cursos têm a classe do canónico.
    for table in ("source", "document"):
        op.add_column(
            table,
            sa.Column("license_class", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        )
        # NOT VALID e depois VALIDATE: numa tabela grande (`document`) a validação corre sem o
        # bloqueio exclusivo que um CHECK normal pediria durante a varredura.
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_license_class"
            f" CHECK ({LICENSE_CHECK}) NOT VALID"
        )
    op.execute(
        "UPDATE source SET license_class = course.license_class"
        " FROM course WHERE source.id = 'course:' || course.id"
    )
    op.execute(
        "UPDATE document SET license_class = source.license_class"
        " FROM source WHERE source.id = document.source_id AND source.license_class = 'private'"
    )
    for table in ("source", "document"):
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT ck_{table}_license_class")
    # Marca persistente de «privada» (`ariadne licenses mark-private`): a sincronização das fontes
    # reescreve `license_class` a partir do objeto Source, e a classe mais restritiva ganha sempre,
    # por isso um ZIM marcado privado nunca volta a aberto sozinho.
    op.add_column("source", sa.Column("license_override", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_source_license_override"),
        "source",
        "license_override IS NULL OR license_override = 'private'",
    )
    op.alter_column("source", "license_class", server_default=None)
    op.alter_column("document", "license_class", server_default=None)
    # Os documentos privados são poucos: listar, contar e bloquear faz-se por este índice.
    op.create_index(
        "ix_document_private",
        "document",
        ["source_id"],
        postgresql_where=sa.text("license_class = 'private'"),
    )

    # A herança também vive na base de dados (rede de segurança): um documento de uma fonte
    # privada é privado, e uma fonte que passa a privada leva os seus documentos atrás.
    op.execute(
        """
        CREATE FUNCTION ariadne_document_license() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.license_class <> 'private' AND EXISTS (
                SELECT 1 FROM source WHERE id = NEW.source_id AND license_class = 'private'
            ) THEN
                NEW.license_class := 'private';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_document_license BEFORE INSERT OR UPDATE OF license_class, source_id"
        " ON document FOR EACH ROW EXECUTE FUNCTION ariadne_document_license()"
    )
    op.execute(
        """
        CREATE FUNCTION ariadne_source_license() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE document SET license_class = 'private'
             WHERE source_id = NEW.id AND license_class <> 'private';
            RETURN NEW;
        END
        $$
        """
    )
    # Só quando a classe **muda** para privado: o registo de fontes reescreve a coluna a cada
    # sincronização, e varrer os documentos de uma fonte (milhões, nos ZIM) de cada vez custaria.
    op.execute(
        "CREATE TRIGGER trg_source_license_insert AFTER INSERT ON source"
        " FOR EACH ROW WHEN (NEW.license_class = 'private')"
        " EXECUTE FUNCTION ariadne_source_license()"
    )
    op.execute(
        "CREATE TRIGGER trg_source_license_update AFTER UPDATE OF license_class ON source"
        " FOR EACH ROW WHEN (NEW.license_class = 'private'"
        " AND OLD.license_class IS DISTINCT FROM NEW.license_class)"
        " EXECUTE FUNCTION ariadne_source_license()"
    )

    # O que as saídas recusaram (`blocked`) ou deixaram de fora (`filtered`): o módulo Licenças.
    op.create_table(
        "license_block",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("items", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column(
            "sample",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('blocked', 'filtered')", name=op.f("ck_license_block_action")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_license_block")),
    )
    op.create_index("ix_license_block_created_at", "license_block", [sa.text("created_at DESC")])
    op.create_table(
        "book",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "authors",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("published", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "identifiers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.Text(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("calibre_id", sa.Integer(), nullable=True),
        sa.Column("license_class", sa.Text(), nullable=False),
        sa.Column("license_name", sa.Text(), nullable=False),
        sa.Column("attribution", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("chapters", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("characters", sa.BigInteger(), server_default=ZERO, nullable=False),
        sa.Column("extractor_version", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("extraction_note", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("kavita_library_id", sa.Integer(), nullable=True),
        sa.Column("kavita_series_id", sa.Integer(), nullable=True),
        sa.Column("kavita_chapter_id", sa.Integer(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(LICENSE_CHECK, name=op.f("ck_book_license_class")),
        sa.CheckConstraint("format IN ('epub', 'pdf')", name=op.f("ck_book_format")),
        sa.CheckConstraint("status IN ('active', 'removed')", name=op.f("ck_book_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_book")),
    )
    op.create_index("ix_book_file_sha256", "book", ["file_sha256"], unique=True)
    op.create_index(
        "ix_book_calibre_id", "book", ["calibre_id"], unique=True,
        postgresql_where=sa.text("calibre_id IS NOT NULL"),
    )  # fmt: skip
    op.create_table(
        "chapter",
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("book_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("href", sa.Text(), nullable=True),
        sa.Column("start_page", sa.Integer(), nullable=True),
        sa.Column("end_page", sa.Integer(), nullable=True),
        sa.Column("characters", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("search_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', search_text)", persisted=True),
            nullable=False,
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("held_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name=op.f("fk_chapter_document_id_document")
        ),
        sa.ForeignKeyConstraint(["book_id"], ["book.id"], name=op.f("fk_chapter_book_id_book")),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_chapter")),
    )
    op.create_index("ix_chapter_book_id_position", "chapter", ["book_id", "position"])
    op.create_index("ix_chapter_search_tsv", "chapter", ["search_tsv"], postgresql_using="gin")


def downgrade() -> None:
    # Os livros não existem antes da 0011: saem as fontes `book` e tudo o que deriva dos seus
    # capítulos (ligações, vetores, chunks, curadoria, registos de consulta e os documentos).
    chapters = (
        "SELECT d.id FROM document d JOIN source s ON s.id = d.source_id WHERE s.kind = 'book'"
    )
    op.drop_table("chapter")
    op.execute(
        f"DELETE FROM link WHERE src_document IN ({chapters}) OR dst_document IN ({chapters})"
    )
    op.execute(f"DELETE FROM query_result WHERE document_id IN ({chapters})")
    op.execute(
        "DELETE FROM chunk_vector WHERE chunk_id IN"
        f" (SELECT id FROM chunk WHERE document_id IN ({chapters}))"
    )
    for table in ("chunk", "document_vector", "curation"):
        op.execute(f"DELETE FROM {table} WHERE document_id IN ({chapters})")
    op.execute(f"DELETE FROM document WHERE id IN ({chapters})")
    op.execute("DELETE FROM source WHERE kind = 'book'")
    op.drop_table("book")
    op.drop_table("license_block")
    op.execute("DROP TRIGGER trg_source_license_update ON source")
    op.execute("DROP TRIGGER trg_source_license_insert ON source")
    op.execute("DROP FUNCTION ariadne_source_license()")
    op.execute("DROP TRIGGER trg_document_license ON document")
    op.execute("DROP FUNCTION ariadne_document_license()")
    op.drop_index("ix_document_private", table_name="document")
    op.drop_constraint(op.f("ck_document_license_class"), "document", type_="check")
    op.drop_column("document", "license_class")
    op.drop_constraint(op.f("ck_source_license_class"), "source", type_="check")
    op.drop_constraint(op.f("ck_source_license_override"), "source", type_="check")
    op.drop_column("source", "license_override")
    op.drop_column("source", "license_class")
