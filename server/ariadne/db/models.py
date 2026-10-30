"""Modelo de dados (ARCHITECTURE.md, secção 10). A migração 0001 cria exatamente isto."""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Double,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Dimensão do modelo de embeddings atual; outra dimensão usa tabela própria (secção 10).
EMBEDDING_DIM = 384

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Constantes ao nível do módulo: dentro de Chunk, `text` é o nome de uma coluna.
NOW = text("now()")
ZERO = text("0")
EMPTY_JSON = text("'{}'::jsonb")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Source(Base):
    """Fonte plugável; o ZIM é a primeira (ex.: 'zim:wikipedia_pt_all_nopic')."""

    __tablename__ = "source"
    __table_args__ = (
        CheckConstraint("license_class IN ('open', 'private')", name="license_class"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # 'zim' | 'pdf' | 'vault' | ...
    title: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)  # ISO 639
    version: Mapped[str] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text)
    wing: Mapped[str] = mapped_column(Text)  # ala MemPalace
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    # Classe de licença (STRUCTURE.md, secção 7): `open` ou `private`. Sem valor por omissão: quem
    # cria a fonte tem de a dizer (os ZIM são abertos; livros e cursos declaram-na).
    license_class: Mapped[str] = mapped_column(Text)
    # `private` = marcada à mão como privada (`licenses mark-private`): a classe mais restritiva
    # ganha sempre, por isso a sincronização das fontes nunca a volta a pôr aberta.
    license_override: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Room(Base):
    """Sala MemPalace: um grupo de artigos de uma ala (fonte) com temas próximos. O centróide é o
    do k-means esférico sobre os vetores de artigo (`document_vector`) do modelo `model_id`; o
    rótulo vem dos títulos mais centrais. O `id` sobrevive aos recálculos (a sala nova herda o da
    antiga com o centróide mais parecido)."""

    __tablename__ = "room"
    __table_args__ = (Index("ix_room_wing", "wing"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wing: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)  # '<repo>@<revisão>' dos vetores do centróide
    centroid: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    size: Mapped[int] = mapped_column(Integer, server_default=ZERO)  # documentos agora
    # Documentos da ala quando as salas foram calculadas: o crescimento mede-se contra isto.
    base_size: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    # Uma sala só é visível (listas, pesquisa, espelho) quando o cálculo da ala acaba: até lá as
    # novas ficam `ready = false`, sem rótulo nem tamanho certos.
    ready: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    # Só anda quando o rótulo ou o centróide mudam (o espelho MemPalace refaz a ala por isso).
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Concept(Base):
    """Agrupa o mesmo tema em várias línguas e fontes (QID do Wikidata quando existe)."""

    __tablename__ = "concept"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wikidata_qid: Mapped[str | None] = mapped_column(Text, unique=True)
    origin_lang: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    # Como se soube a língua original: 'manual' (curadoria) ou 'country:XX' (TSV de países).
    origin_source: Mapped[str | None] = mapped_column(Text)


class Document(Base):
    """ID estável '<source_id>/<path>'; a versão fica em coluna separada (secção 14.1)."""

    __tablename__ = "document"
    __table_args__ = (
        CheckConstraint("content_type IN ('article', 'lesson', 'chapter')", name="content_type"),
        CheckConstraint("license_class IN ('open', 'private')", name="license_class"),
        # Os documentos privados (poucos): listar, contar e bloquear faz-se por aqui.
        Index(
            "ix_document_private",
            "source_id",
            postgresql_where=text("license_class = 'private'"),
        ),
        # `title_match`: títulos (e títulos alternativos) normalizados em comum, por interseção.
        Index("ix_document_title_keys", "title_keys", postgresql_using="gin"),
        # O que o `linker` ainda tem de ver: percorre-se só isto.
        Index("ix_document_unlinked", "id", postgresql_where=text("linked_at IS NULL")),
        # Os documentos de um conceito (`translations`, regra histórica).
        Index("ix_document_concept_id", "concept_id"),
        # Os documentos de uma sala (vizinhos, filtro da pesquisa, tamanhos).
        Index("ix_document_room", "room"),
        # O caminho da página no Wiki.js (`meta.wiki_path`): reservado antes de escrever, único.
        Index(
            "ix_document_wiki_path",
            text("(meta ->> 'wiki_path')"),
            unique=True,
            postgresql_where=text("(meta ->> 'wiki_path') IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"))
    path: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    concept_id: Mapped[int | None] = mapped_column(ForeignKey("concept.id"))
    # Sala (secção 4.5): o agrupamento dos vetores de artigo da ala; NULL = ainda sem sala.
    room: Mapped[int | None] = mapped_column(ForeignKey("room.id", ondelete="SET NULL"))
    wiki_page_id: Mapped[int | None] = mapped_column(Integer)
    # O que é (STRUCTURE.md, secção 4): `article` (Wiki.js), `lesson` (curso) ou `chapter` (livro).
    content_type: Mapped[str] = mapped_column(Text, server_default=text("'article'"))
    # Classe de licença (secção 7), herdada da fonte (e por tudo o que deriva do documento: chunks,
    # vetores, ligações, registos). Sem valor por omissão; o Postgres impede um documento aberto
    # numa fonte privada (`trg_document_license`).
    license_class: Mapped[str] = mapped_column(Text)
    heat: Mapped[float] = mapped_column(Double, server_default=ZERO)
    heat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    # Quando o worker `neighbors` expandiu a vizinhança deste documento (secção 6.2); NULL = nunca.
    expanded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Chaves de `title_match` (secção 7.2): o título e os títulos alternativos (`meta.aliases`)
    # normalizados. Quem as preenche é o `linker`, antes de ligar o documento.
    title_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    # Quando o `linker` viu o documento pela última vez; NULL = por ver (novo, ou mudou o título,
    # os títulos alternativos ou o vetor de artigo).
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # O linker reclama o documento com uma concessão e só o dá por visto no fim; a concessão caduca
    # (worker morto) e os que falham seguidamente deixam de ser tentados (`link_attempts`).
    link_lease: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    link_attempts: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)


class Curation(Base):
    """Estado da curadoria de um documento (STRUCTURE.md, secção 5): o que a pipeline decidiu e o
    que está publicado no Wiki.js. Uma linha por documento; uma versão nova do texto (outra
    `fingerprint`) volta a avaliar tudo e guarda o texto antigo publicado até ao novo passar."""

    __tablename__ = "curation"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('published', 'pending_curator', 'needs_review', 'rejected')",
            name="decision",
        ),
        # A fila de revisão: por estado, dos mais recentes para os mais antigos.
        Index("ix_curation_decision_updated", "decision", text("updated_at DESC"), "document_id"),
        # O ecrã de revisão em lote: os itens que vieram de uma corrida externa.
        Index(
            "ix_curation_batch_run",
            "batch_run_id",
            "decision",
            postgresql_where=text("batch_run_id IS NOT NULL"),
        ),
    )

    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"), primary_key=True)
    source_version: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)  # sha256 do texto limpo (`original_md`)
    flags: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    sampled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)  # porque está neste estado
    curator_model: Mapped[str | None] = mapped_column(Text)
    fidelity: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    original_md: Mapped[str] = mapped_column(Text)  # conversão determinística (sem Ligações)
    curated_md: Mapped[str | None] = mapped_column(Text)  # a reescrita do curador
    published_md: Mapped[str | None] = mapped_column(Text)  # o corpo que está no Wiki.js
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A corrida de GPU externa que produziu o `curated_md` (para o ecrã de revisão em lote).
    batch_run_id: Mapped[int | None] = mapped_column(ForeignKey("batch_run.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Course(Base):
    """Índice de um curso do formato canónico (Git): o que a app e as consultas precisam sem abrir o
    repositório. A fonte de verdade é o repositório (`COURSES_REPO`); isto reconstrói-se dele
    (`ariadne courses sync`)."""

    __tablename__ = "course"
    __table_args__ = (CheckConstraint("status IN ('active', 'archived')", name="status"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # o slug do curso
    title: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)  # língua do texto
    programming_language: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, server_default=text("''"))
    origin_source_id: Mapped[str | None] = mapped_column(Text)  # a fonte de onde veio
    license_name: Mapped[str] = mapped_column(Text)
    license_class: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    attribution: Mapped[str] = mapped_column(Text)
    current_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class CourseVersion(Base):
    """Uma versão de um curso: o commit onde está no Git e o estado da publicação no Moodle. Uma
    versão nova da fonte é uma linha nova; as anteriores ficam."""

    __tablename__ = "course_version"
    __table_args__ = (
        CheckConstraint(
            "publish_status IN ('unpublished', 'published', 'outdated', 'failed')",
            name="publish_status",
        ),
    )

    course_id: Mapped[str] = mapped_column(ForeignKey("course.id"), primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    source_version: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)  # do conteúdo (`Course.fingerprint`)
    git_commit: Mapped[str | None] = mapped_column(Text)  # HEAD do repositório quando se indexou
    modules: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    lessons: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    exercises: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    tests: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    unmapped_tests: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    # unpublished | published | outdated (o canónico mudou depois de publicar) | failed
    publish_status: Mapped[str] = mapped_column(Text, server_default=text("'unpublished'"))
    publish_backend: Mapped[str | None] = mapped_column(Text)  # 'moodle'
    published_fingerprint: Mapped[str | None] = mapped_column(Text)  # o que está na plataforma
    platform_course_id: Mapped[int | None] = mapped_column(Integer)  # id do curso no Moodle
    platform_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_error: Mapped[str | None] = mapped_column(Text)
    publish_report: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)


class Lesson(Base):
    """Uma lição da versão atual de um curso; é também um `document` (pesquisa, grafo, `read`)."""

    __tablename__ = "lesson"
    __table_args__ = (
        Index("ix_lesson_course_id_position", "course_id", "position"),
        # A pesquisa de texto nas lições (fallback, quando o nível 3 não chega).
        Index(
            "ix_lesson_search_text",
            text("to_tsvector('simple', search_text)"),
            postgresql_using="gin",
        ),
    )

    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("course.id"))
    version: Mapped[str] = mapped_column(Text)
    module_id: Mapped[str] = mapped_column(Text)
    module_title: Mapped[str] = mapped_column(Text)
    lesson_id: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)  # ordem no curso
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, server_default=text("''"))
    requires: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    search_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    git_path: Mapped[str] = mapped_column(Text)  # onde está no repositório
    # Onde ficou no Moodle (cmid e URL), preenchido pela publicação.
    platform_activity_id: Mapped[int | None] = mapped_column(Integer)
    platform_kind: Mapped[str | None] = mapped_column(Text)  # 'page' | 'quiz'
    platform_url: Mapped[str | None] = mapped_column(Text)
    # Retida pela curadoria (flags por rever): não vai para a plataforma enquanto isto estiver.
    held_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Exercise(Base):
    """Um exercício de uma lição: o resumo dos testes extraídos (o código fica no canónico)."""

    __tablename__ = "exercise"
    __table_args__ = (Index("ix_exercise_course_id", "course_id"),)

    document_id: Mapped[str] = mapped_column(ForeignKey("lesson.document_id"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)  # o id do exercício na lição
    course_id: Mapped[str] = mapped_column(ForeignKey("course.id"))
    title: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    test_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    mapped_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)  # testes no CodeRunner
    gradable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    has_solution: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # [{id, description, mapped, unmapped_reason}]: o que a app mostra dos testes extraídos.
    tests: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    platform_question_id: Mapped[int | None] = mapped_column(Integer)  # id da pergunta no Moodle


class LicenseBlock(Base):
    """Uma saída que recusou (`blocked`) ou deixou de fora (`filtered`) conteúdo privado."""

    __tablename__ = "license_block"
    __table_args__ = (
        CheckConstraint("action IN ('blocked', 'filtered')", name="action"),
        Index("ix_license_block_created_at", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    output: Mapped[str] = mapped_column(Text)  # a saída: «vectors build --out», «release», …
    action: Mapped[str] = mapped_column(Text)
    items: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    # [{kind, id, because}]: alguns dos itens, para o utilizador perceber o que foi.
    sample: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))


class Book(Base):
    """Um livro da biblioteca (Calibre): o que a app e as consultas precisam sem abrir o ficheiro.
    A fonte de verdade é a biblioteca do Calibre (volume partilhado); isto reconstrói-se dela
    (`ariadne books sync`). Cada livro é uma fonte (`book:<id>`) e cada capítulo um documento."""

    __tablename__ = "book"
    __table_args__ = (
        CheckConstraint("license_class IN ('open', 'private')", name="license_class"),
        CheckConstraint("format IN ('epub', 'pdf')", name="format"),
        CheckConstraint("status IN ('active', 'removed')", name="status"),
        Index("ix_book_file_sha256", "file_sha256", unique=True),
        Index(
            "ix_book_calibre_id",
            "calibre_id",
            unique=True,
            postgresql_where=text("calibre_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # o slug; a fonte é `book:<id>`
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    language: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    published: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    identifiers: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    format: Mapped[str] = mapped_column(Text)  # o formato que se extrai: 'epub' | 'pdf'
    file_path: Mapped[str] = mapped_column(Text)  # relativo à biblioteca (`BOOKS_DIR`)
    file_sha256: Mapped[str] = mapped_column(Text)  # do ficheiro extraído: impede duplicados
    file_size: Mapped[int] = mapped_column(BigInteger)
    calibre_id: Mapped[int | None] = mapped_column(Integer)
    license_class: Mapped[str] = mapped_column(Text)  # declarada ao registar; sem omissão
    license_name: Mapped[str] = mapped_column(Text)
    attribution: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chapters: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    pages: Mapped[int | None] = mapped_column(Integer)
    characters: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    extractor_version: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    # Como se dividiu em capítulos (TOC/índice, ou heurística por páginas) e avisos da extração.
    extraction_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    # Onde está no Kavita (preenchido depois de uma leitura da biblioteca); o URL calcula-se.
    kavita_library_id: Mapped[int | None] = mapped_column(Integer)
    kavita_series_id: Mapped[int | None] = mapped_column(Integer)
    kavita_chapter_id: Mapped[int | None] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)


class BookChapter(Base):
    """Um capítulo de um livro; é também um `document` (pesquisa, grafo, `read`). O texto
    extraído fica em `search_text` (pesquisa de texto e citações por posição)."""

    __tablename__ = "chapter"
    __table_args__ = (
        Index("ix_chapter_book_id_position", "book_id", "position"),
        # A pesquisa de texto nos capítulos (nível 2 dos livros): o tsvector guarda-se (coluna
        # gerada), porque os capítulos são grandes e ordenar por relevância recalculá-lo seria caro.
        Index("ix_chapter_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"), primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("book.id"))
    position: Mapped[int] = mapped_column(Integer)  # ordem no livro, a contar de 1
    path: Mapped[str] = mapped_column(Text)  # o caminho do documento dentro da fonte
    title: Mapped[str] = mapped_column(Text)
    href: Mapped[str | None] = mapped_column(Text)  # EPUB: o ficheiro do capítulo
    start_page: Mapped[int | None] = mapped_column(Integer)  # PDF: páginas (a contar de 1)
    end_page: Mapped[int | None] = mapped_column(Integer)
    characters: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    search_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', search_text)", persisted=True)
    )
    content_hash: Mapped[str] = mapped_column(Text)
    # Retido pela curadoria (flags por rever): continua pesquisável, mas assinalado.
    held_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class DocumentVector(Base):
    """Vetor por artigo (título + 1.º parágrafo), para ligações e vizinhança."""

    __tablename__ = "document_vector"
    __table_args__ = (
        # Vizinhos semânticos e ligações `vector` (kNN por cosseno).
        Index(
            "ix_document_vector_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"), primary_key=True)
    model_id: Mapped[str] = mapped_column(Text, primary_key=True)  # '<repo>@<revisão>'
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))


class Chunk(Base):
    __tablename__ = "chunk"
    __table_args__ = (UniqueConstraint("document_id", "ord"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"))
    ord: Mapped[int] = mapped_column(Integer)  # posição no documento
    text: Mapped[str] = mapped_column(Text)
    heat: Mapped[float] = mapped_column(Double, server_default=ZERO)
    heat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class ChunkVector(Base):
    __tablename__ = "chunk_vector"
    __table_args__ = (
        Index(
            "ix_chunk_vector_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # O orçamento diário conta os vetores de hoje: `count_vectors_since`.
        Index("ix_chunk_vector_model_id_created_at", "model_id", "created_at"),
    )

    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunk.id"), primary_key=True)
    model_id: Mapped[str] = mapped_column(Text, primary_key=True)  # '<repo>@<revisão>'
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Link(Base):
    """Aresta auditável do grafo (secção 7.3)."""

    __tablename__ = "link"
    __table_args__ = (
        UniqueConstraint("src_document", "dst_document", "kind"),
        Index("ix_link_dst_document", "dst_document"),  # as arestas valem nos dois sentidos
        # Fila de curadoria: pela pontuação e por id (paginação por cursor, sem ordenar).
        Index(
            "ix_link_proposed",
            text("score DESC NULLS LAST"),
            "id",
            postgresql_where=text("status = 'proposed'"),
        ),
        # `zim_internal` («A cita B») e `requires` («A requer B») têm sentido: guardam-se como são.
        # As outras arestas são não dirigidas e guardam-se com src < dst.
        CheckConstraint(
            "kind IN ('zim_internal', 'requires') "
            'OR src_document COLLATE "C" < dst_document COLLATE "C"',
            name="undirected_order",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    src_document: Mapped[str] = mapped_column(ForeignKey("document.id"))
    dst_document: Mapped[str] = mapped_column(ForeignKey("document.id"))
    # zim_internal | title_match | wikidata | vector | langlink | manual | requires
    kind: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(REAL)
    model_id: Mapped[str | None] = mapped_column(Text)
    # proposed | accepted | rejected
    status: Mapped[str] = mapped_column(Text, server_default=text("'accepted'"))
    created_by: Mapped[str | None] = mapped_column(Text)  # worker ou utilizador
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    # Quem aceitou ou rejeitou a aresta (curadoria, secção 11.2), e quando.
    reviewed_by: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Principal(Base):
    """Identidade de quem chama a API/MCP: utilizador ou agente (secção 14.4)."""

    __tablename__ = "principal"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="role"),
        CheckConstraint("scope IN ('full', 'mcp')", name="scope"),
        CheckConstraint(
            "scope = 'full' OR (kind = 'agent' AND role = 'member')", name="scope_mcp_member"
        ),
        # Suspensão e quota são só das chaves `mcp` (clientes externos).
        CheckConstraint("suspended_at IS NULL OR scope = 'mcp'", name="suspended_mcp"),
        CheckConstraint(
            "monthly_query_quota IS NULL OR (scope = 'mcp' AND monthly_query_quota > 0)",
            name="quota_mcp",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # 'user' | 'agent'
    name: Mapped[str] = mapped_column(Text)
    # Papel (STRUCTURE.md, secção 11): 'admin' gere chaves e principais; 'member' usa a biblioteca.
    role: Mapped[str] = mapped_column(Text, server_default=text("'member'"))
    # Âmbito da chave: 'full' (a API e o MCP, com tudo o que o papel deixa) ou 'mcp' (clientes
    # externos da versão alojada: só o MCP e só o conteúdo da classe aberto).
    scope: Mapped[str] = mapped_column(Text, server_default=text("'full'"))
    api_key_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    # Quando a chave foi revogada (o hash apaga-se; as linhas de `tool_call` e `query_log` ficam).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Suspensa: a chave mantém o hash (o mesmo valor) mas responde 403 até ser retomada.
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Id do cliente no portal: torna o `POST /api/principals` idempotente (único se existir).
    external_id: Mapped[str | None] = mapped_column(Text, unique=True)
    # Pedidos (chamadas a ferramentas) por mês UTC que a chave `mcp` pode fazer; None = sem quota.
    monthly_query_quota: Mapped[int | None] = mapped_column(Integer)


class Collection(Base):
    """Uma coleção a pré-aquecer: a lista de caminhos/títulos que o utilizador pediu para indexar
    (`POST /api/collections`), com o resultado de a pôr na fila (secção 12, Ligações)."""

    __tablename__ = "collection"
    # O limite de coleções por hora conta as do principal (`POST /api/collections`).
    __table_args__ = (Index("ix_collection_principal_id_created_at", "principal_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"))
    entries: Mapped[list[str]] = mapped_column(ARRAY(Text))  # como o utilizador os escreveu
    priority: Mapped[float] = mapped_column(REAL)
    queued: Mapped[int] = mapped_column(Integer)  # documentos postos na fila de indexação
    known: Mapped[int] = mapped_column(Integer)  # já indexados, ou com falha recente
    ignored: Mapped[list[str]] = mapped_column(ARRAY(Text))  # entradas que não são artigos
    created_by: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[int | None] = mapped_column(ForeignKey("principal.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class QueryLog(Base):
    """Registo de consultas: estatística, aquecimento do índice e dados de treino."""

    __tablename__ = "query_log"
    # As estatísticas (`/api/stats`) agregam por intervalo de datas.
    __table_args__ = (Index("ix_query_log_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    principal_id: Mapped[int | None] = mapped_column(ForeignKey("principal.id"))
    query: Mapped[str] = mapped_column(Text)
    query_lang: Mapped[str | None] = mapped_column(Text)
    filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    level_served: Mapped[int | None] = mapped_column(SmallInteger)  # 1, 2 ou 3
    top_score: Mapped[float | None] = mapped_column(REAL)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    session_id: Mapped[str | None] = mapped_column(Text)  # agrupa interações de um agente
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class QueryResult(Base):
    __tablename__ = "query_result"

    query_id: Mapped[int] = mapped_column(ForeignKey("query_log.id"), primary_key=True)
    rank: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id"))
    chunk_id: Mapped[int | None] = mapped_column(ForeignKey("chunk.id"))
    level: Mapped[int] = mapped_column(SmallInteger)
    score: Mapped[float | None] = mapped_column(REAL)
    used: Mapped[bool | None]  # foi citado/aberto pelo agente?
    feedback: Mapped[int | None] = mapped_column(SmallInteger)  # -1, 0, +1
    # A versão na língua original do conceito (regra histórica), acrescentada ou assinalada.
    original_source: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Só a pergunta traduzida (LibreTranslate) a encontrou.
    translated_query: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class ToolCall(Base):
    """Uma chamada a uma ferramenta MCP ou endpoint REST: quem, o quê, quanto demorou, se falhou.

    `result` guarda só ids e contagens, nunca o texto devolvido. As sessões completas para treino
    (pergunta, chamadas, resultados, resposta) são a decisão 14.7 e ficam para a Fase 10.
    """

    __tablename__ = "tool_call"
    # O uso por chave e a quota mensal contam as chamadas de um principal num intervalo.
    __table_args__ = (
        Index("ix_tool_call_created_at", "created_at"),
        Index("ix_tool_call_principal_id_created_at", "principal_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principal.id"))
    session_id: Mapped[str | None] = mapped_column(Text, index=True)  # o do MCP, quando existe
    tool: Mapped[str] = mapped_column(Text)  # 'search' | 'read' | 'sources' | 'feedback'
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    query_id: Mapped[int | None] = mapped_column(ForeignKey("query_log.id"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Job(Base):
    """Fila de trabalho genérica no Postgres (SELECT … FOR UPDATE SKIP LOCKED); ver `queue.py`."""

    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_status_priority_run_after", "status", text("priority DESC"), "run_after"),
        # Fila de espera: o que o `claim` percorre.
        Index(
            "ix_job_pending_priority_run_after",
            text("priority DESC"),
            "run_after",
            postgresql_where=text("status = 'pending'"),
        ),
        # No máximo um job pendente por chave: enfileirar de novo sobe a prioridade.
        Index(
            "uq_job_pending_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # `recently_failed`: os jobs falhados de uma chave.
        Index("ix_job_failed_dedupe_key", "dedupe_key", postgresql_where=text("status = 'failed'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # index_document | neighbors | import_page | link | catalog_sync
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    priority: Mapped[float] = mapped_column(REAL, server_default=ZERO)
    # pending | running | done | failed
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str | None] = mapped_column(Text)


class BackupRun(Base):
    """Um backup local (`kind = backup`) ou uma release externa (`release`): o que o dashboard
    mostra em «Operação» (último, tamanho, ensaio, próximo agendamento). Nada de conteúdo aqui."""

    __tablename__ = "backup_run"
    __table_args__ = (
        CheckConstraint("kind IN ('backup', 'release')", name="kind"),
        CheckConstraint("status IN ('running', 'ok', 'failed')", name="status"),
        CheckConstraint(
            "rehearsal_status IS NULL OR rehearsal_status IN ('ok', 'failed')",
            name="rehearsal_status",
        ),
        Index("ix_backup_run_kind_started", "kind", text("started_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)  # a pasta (backup-… / ariadne-release-…)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    parts: Mapped[int | None] = mapped_column(Integer)
    content_sha256: Mapped[str | None] = mapped_column(Text)
    manifest_sha256: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_url: Mapped[str | None] = mapped_column(Text)
    # O ensaio de restauro (releases): feito por quem tem a chave privada, fora do sistema.
    rehearsal_status: Mapped[str | None] = mapped_column(Text)
    rehearsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rehearsal_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ComputeControl(Base):
    """O painel de controlo da curadoria (uma só linha, `id = 1`): estado (play/pausa), destino,
    janela agendada e a configuração de cada destino. Nunca guarda credenciais."""

    __tablename__ = "compute_control"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("state IN ('stopped', 'running', 'paused')", name="state"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    destination: Mapped[str] = mapped_column(Text, server_default=text("'local'"))
    fallback_local: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    window_start: Mapped[str] = mapped_column(Text, server_default=text("'23:00'"))
    window_end: Mapped[str] = mapped_column(Text, server_default=text("'07:00'"))
    timezone: Mapped[str] = mapped_column(Text, server_default=text("'UTC'"))
    manual_override: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    disabled_kinds: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    batch_size: Mapped[int] = mapped_column(Integer, server_default=text("200"))
    max_run_hours: Mapped[float] = mapped_column(Float, server_default=text("8"))
    destinations: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    external_blocked_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_by: Mapped[str | None] = mapped_column(Text)


class BatchRun(Base):
    """Uma corrida de lote num destino externo (Kaggle, Colab manual, …): as tarefas exportadas,
    o estado no destino, as horas de GPU gastas e o resultado da importação."""

    __tablename__ = "batch_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'queued', 'running', 'awaiting_results', 'importing', "
            "'done', 'failed', 'cancelled')",
            name="status",
        ),
        Index("ix_batch_run_created", text("created_at DESC")),
        Index(
            "uq_batch_run_in_progress",
            text("(true)"),
            unique=True,
            postgresql_where=text(
                "status IN ('created', 'queued', 'running', 'awaiting_results', 'importing')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    backend: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'created'"))
    manual: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    kinds: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    model: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(Text)
    task_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    request_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    applied_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    review_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    invalid_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    released_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    external_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    deadline_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    running_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gpu_seconds: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    # O JSONL de pedidos e o de resultados, comprimidos (apagam-se quando a corrida acaba).
    tasks_gz: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    results_gz: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    # O túnel (`ariadne.compute.gateway`): o SHA-256 do token desta corrida, a validade, e o que o
    # notebook comunicou no último *heartbeat*.
    token_hash: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)


class BatchTask(Base):
    """Uma tarefa (um job da fila) numa corrida: o que se exportou e o que lhe aconteceu."""

    __tablename__ = "batch_task"
    __table_args__ = (
        Index("ix_batch_task_run", "run_id"),
        Index("ix_batch_task_job", "job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("batch_run.id"))
    job_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    request_ids: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # exported | applied | review | invalid | missing
    status: Mapped[str] = mapped_column(Text, server_default=text("'exported'"))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Domain(Base):
    """Um nó da árvore de domínios do Wiki.js (docs/WIKI-IMPORT.md, secção 6). Vem de
    `ariadne/wiki/domains.yaml` (`domains.sync_tree`); os nós que saem do YAML ficam inativos."""

    __tablename__ = "domain"
    __table_args__ = (Index("ix_domain_parent_id", "parent_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # 'saude', 'saude/medicamentos'
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("domain.id"))
    position: Mapped[int] = mapped_column(Integer, server_default=ZERO)  # ordem entre irmãos
    names: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=EMPTY_JSON)  # por locale
    slugs: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    descriptions: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class DomainVector(Base):
    """O vetor da descrição de um domínio, por modelo (como `DocumentVector`)."""

    __tablename__ = "domain_vector"

    domain_id: Mapped[str] = mapped_column(ForeignKey("domain.id"), primary_key=True)
    model_id: Mapped[str] = mapped_column(Text, primary_key=True)  # '<repo>@<revisão>'
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))


class DomainAssignment(Base):
    """Onde o documento está na árvore: o domínio, com que confiança e porquê."""

    __tablename__ = "domain_assignment"
    __table_args__ = (
        CheckConstraint(
            "method IN ('prior', 'vector', 'structure', 'llm', 'human', 'fallback')",
            name="method",
        ),
        Index("ix_domain_assignment_domain_id", "domain_id"),
        # A fila de revisão: percorre-se só isto.
        Index(
            "ix_domain_assignment_review",
            "document_id",
            postgresql_where=text("needs_review"),
        ),
    )

    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"), primary_key=True)
    domain_id: Mapped[str] = mapped_column(ForeignKey("domain.id"))
    confidence: Mapped[float] = mapped_column(Double, server_default=ZERO)
    method: Mapped[str] = mapped_column(Text)  # prior | vector | structure | llm | human | fallback
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON)
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class TranslationCache(Base):
    """Traduções já feitas (LibreTranslate): a mesma frase não se traduz duas vezes."""

    __tablename__ = "translation_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)  # sha256 de língua, termos e texto
    source_lang: Mapped[str] = mapped_column(Text)
    target_lang: Mapped[str] = mapped_column(Text)
    translated: Mapped[str] = mapped_column(Text)
    backend: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class IntegrationSetting(Base):
    """Um valor do cofre das integrações (`ariadne.vault`): sempre cifrado (AES-GCM) com a chave
    mestra `ARIADNE_MASTER_KEY`, segredo ou não. `key` é o nome da definição em minúsculas
    (`calibre_api_key`); as linhas `_…` são internas (a verificação da chave mestra)."""

    __tablename__ = "integration_setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    integration: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    is_secret: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_by: Mapped[str | None] = mapped_column(Text)


class IntegrationAudit(Base):
    """Quem mudou o quê no cofre e quando. Nunca leva o valor."""

    __tablename__ = "integration_audit"
    __table_args__ = (Index("ix_integration_audit_at", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    actor: Mapped[str] = mapped_column(Text)  # nome do principal, ou «cli:<utilizador>»
    action: Mapped[str] = mapped_column(Text)  # set | rotate | generate | delete | import | test
    key: Mapped[str | None] = mapped_column(Text)
    integration: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
