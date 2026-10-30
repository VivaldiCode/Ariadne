"""Fase 10: base de cursos — índice dos cursos canónicos, tipo de conteúdo, arestas `requires`

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-28 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
ZERO = sa.text("0")


def upgrade() -> None:
    # `document.content_type`: artigo (Wiki.js), lição (curso) ou capítulo (livro).
    op.add_column(
        "document",
        sa.Column("content_type", sa.Text(), server_default=sa.text("'article'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_document_content_type"),
        "document",
        "content_type IN ('article', 'lesson', 'chapter')",
    )

    # `requires` («A requer B») é dirigida: guarda-se como é, sem a ordem canónica das outras.
    op.drop_constraint(op.f("ck_link_undirected_order"), "link", type_="check")
    op.create_check_constraint(
        op.f("ck_link_undirected_order"),
        "link",
        "kind IN ('zim_internal', 'requires') "
        'OR src_document COLLATE "C" < dst_document COLLATE "C"',
    )

    op.create_table(
        "course",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("programming_language", sa.Text(), nullable=True),
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("origin_source_id", sa.Text(), nullable=True),
        sa.Column("license_name", sa.Text(), nullable=False),
        sa.Column("license_class", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_course_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_course")),
    )
    op.create_table(
        "course_version",
        sa.Column("course_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("git_commit", sa.Text(), nullable=True),
        sa.Column("modules", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("lessons", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("exercises", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("tests", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("unmapped_tests", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column(
            "publish_status", sa.Text(), server_default=sa.text("'unpublished'"), nullable=False
        ),
        sa.Column("publish_backend", sa.Text(), nullable=True),
        sa.Column("published_fingerprint", sa.Text(), nullable=True),
        sa.Column("platform_course_id", sa.Integer(), nullable=True),
        sa.Column("platform_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_error", sa.Text(), nullable=True),
        sa.Column(
            "publish_report",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "publish_status IN ('unpublished', 'published', 'outdated', 'failed')",
            name=op.f("ck_course_version_publish_status"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["course.id"], name=op.f("fk_course_version_course_id_course")
        ),
        sa.PrimaryKeyConstraint("course_id", "version", name=op.f("pk_course_version")),
    )
    op.create_table(
        "lesson",
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("course_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("module_id", sa.Text(), nullable=False),
        sa.Column("module_title", sa.Text(), nullable=False),
        sa.Column("lesson_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "requires",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("search_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("git_path", sa.Text(), nullable=False),
        sa.Column("platform_activity_id", sa.Integer(), nullable=True),
        sa.Column("platform_kind", sa.Text(), nullable=True),
        sa.Column("platform_url", sa.Text(), nullable=True),
        sa.Column("held_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name=op.f("fk_lesson_document_id_document")
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["course.id"], name=op.f("fk_lesson_course_id_course")
        ),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_lesson")),
    )
    op.create_index("ix_lesson_course_id_position", "lesson", ["course_id", "position"])
    op.create_index(
        "ix_lesson_search_text",
        "lesson",
        [sa.text("to_tsvector('simple', search_text)")],
        postgresql_using="gin",
    )
    op.create_table(
        "exercise",
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("course_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("test_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("mapped_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("gradable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("has_solution", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "tests",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("platform_question_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"], ["lesson.document_id"], name=op.f("fk_exercise_document_id_lesson")
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["course.id"], name=op.f("fk_exercise_course_id_course")
        ),
        sa.PrimaryKeyConstraint("document_id", "key", name=op.f("pk_exercise")),
    )
    op.create_index("ix_exercise_course_id", "exercise", ["course_id"])


def downgrade() -> None:
    op.drop_table("exercise")
    op.drop_table("lesson")
    op.drop_table("course_version")
    op.drop_table("course")
    # As arestas `requires` não cabem na restrição antiga (nem existiam): saem.
    op.execute("DELETE FROM link WHERE kind = 'requires'")
    op.drop_constraint(op.f("ck_link_undirected_order"), "link", type_="check")
    op.create_check_constraint(
        op.f("ck_link_undirected_order"),
        "link",
        'kind = \'zim_internal\' OR src_document COLLATE "C" < dst_document COLLATE "C"',
    )
    op.drop_constraint(op.f("ck_document_content_type"), "document", type_="check")
    op.drop_column("document", "content_type")
