"""Os livros que vêm dentro de um ZIM chegam ao Calibre (`ariadne books import-zim`)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ariadne import cli_books
from ariadne.books.backend import LibraryUnavailable
from ariadne.books.zim_import import (
    Candidate,
    Refused,
    Superseded,
    ZimImportAborted,
    ZimImportError,
    ZimImportReport,
    _write_temp,
    format_of,
    import_zim_books,
)
from ariadne.cli import app as cli
from ariadne.config import Settings
from ariadne.db import models
from ariadne.documents import register_book_candidate
from ariadne.embedder import FakeEmbedder
from ariadne.services import Services
from ariadne.sources.base import NotAnArticle
from ariadne.sources.registry import SourceRegistry
from ariadne.sources.zim import ZimSource
from tests.bookfactory import chapter_body, make_epub, make_pdf
from tests.bookfakes import FakeLibrary
from tests.fakes import default_fake
from tests.zimfactory import make_zim

pytestmark = pytest.mark.db

GUTENBERG = "zim:gutenberg_pt_teste"
WIKIPEDIA = "zim:wikipedia_pt_teste"


def chapters(topic: str) -> list[tuple[str, str]]:
    return [("Um", chapter_body("Um", topic)), ("Dois", chapter_body("Dois", f"{topic} outra vez"))]


def epub_bytes(tmp_path: Path, title: str, topic: str, **kwargs: object) -> bytes:
    file = make_epub(tmp_path / "tmp.epub", chapters(topic), title=title, **kwargs)  # type: ignore[arg-type]
    return file.read_bytes()


def pdf_bytes(tmp_path: Path, title: str) -> bytes:
    pages = [[f"{title} página {n}"] * 6 for n in range(1, 4)]
    return make_pdf(tmp_path / "tmp.pdf", pages, title=title).read_bytes()


EPUB, PDF = "application/epub+zip", "application/pdf"


@pytest.fixture
def make_services(db: sessionmaker[Session], tmp_path: Path) -> Iterator[object]:
    opened: list[Services] = []

    def make(zims: dict[str, dict[str, tuple[str, bytes]]]) -> Services:
        folder = tmp_path / "zim"
        folder.mkdir(exist_ok=True)
        for name, resources in zims.items():
            make_zim(folder / f"{name}_2026-09.zim", name=name, resources=resources)
        library = tmp_path / "biblioteca"
        library.mkdir(exist_ok=True)
        settings = Settings(
            database_url="postgresql+psycopg://x@localhost/x",
            zim_dir=folder,
            books_dir=library,
            kiwix_url="http://kiwix",
        )
        registry = SourceRegistry.from_settings(settings)
        with db() as session:
            registry.sync(session)
        services = Services(
            settings, db, registry, default_fake().client(), embedder=FakeEmbedder()
        )
        services.library = FakeLibrary(library)
        opened.append(services)
        return services

    yield make
    for services in opened:
        services.close()


def gutenberg_resources(tmp_path: Path) -> dict[str, tuple[str, bytes]]:
    return {
        "13 Sonetos.22678.epub": (EPUB, epub_bytes(tmp_path, "13 Sonetos", "Sonetos de amor")),
        "13 Sonetos.22678.pdf": (PDF, pdf_bytes(tmp_path, "13 Sonetos")),
        "Outro Livro.1000.epub": (EPUB, epub_bytes(tmp_path, "Outro Livro", "Outro assunto")),
        "Estragado.1001.epub": (EPUB, b"isto nao e um epub"),
        "So em PDF.1002.pdf": (PDF, pdf_bytes(tmp_path, "So em PDF")),
        "Cifrado.1003.epub": (EPUB, epub_bytes(tmp_path, "Cifrado", "Cifra", drm="adept")),
        "capa.jpg": ("image/jpeg", b"\xff\xd8\xff"),
    }


def library(services: Services) -> FakeLibrary:
    assert isinstance(services.library, FakeLibrary)
    return services.library


def test_formatos_reconhecidos() -> None:
    assert format_of("application/epub+zip", "x") == "epub"
    assert format_of("application/octet-stream", "Livro.PDF") == "pdf"
    assert format_of("", "a/b.mobi") == "mobi"
    assert format_of("image/vnd.djvu", "x") == "djvu"
    assert format_of("text/html", "Lisboa") is None


def test_zim_de_livros_importa_agrupa_formatos_e_relata_falhas(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})
    lines: list[str] = []

    report = import_zim_books(services, GUTENBERG, progress=lines.append)

    assert report.files == 6 and report.books == 5
    assert sorted(b.title for b in report.added) == ["13 Sonetos", "Outro Livro", "So em PDF"]
    sonetos = next(b for b in report.added if b.title == "13 Sonetos")
    assert sonetos.format == "epub"  # o EPUB ganha ao PDF do mesmo <id>
    assert report.duplicates == ["13 Sonetos.22678.pdf"]
    assert {f.path for f in report.failures} == {"Estragado.1001.epub", "Cifrado.1003.epub"}
    reasons = {f.path: f.reason for f in report.failures}
    assert "DRM" in reasons["Cifrado.1003.epub"] or "cifrad" in reasons["Cifrado.1003.epub"]
    assert len(library(services).added) == 3  # só os três livros entraram no Calibre
    assert any("Estragado" in line for line in lines)

    with db() as session:
        books = {b.title: b for b in session.scalars(select(models.Book))}
        assert set(books) == {"13 Sonetos", "Outro Livro", "So em PDF"}
        assert books["13 Sonetos"].format == "epub"
        assert books["So em PDF"].format == "pdf"
        for book in books.values():
            assert book.license_class == "open"
            assert book.license_name == "Public domain (Project Gutenberg)"
        assert "https://www.gutenberg.org/ebooks/22678" in books["13 Sonetos"].attribution
        assert "ZIM gutenberg_pt_teste" in books["13 Sonetos"].attribution

        def meta(path: str) -> dict[str, object]:
            row = session.get(models.Document, f"{GUTENBERG}/{path}")
            assert row is not None
            return dict(row.meta)

        epub = meta("13 Sonetos.22678.epub")
        assert epub["route"] == "book" and epub["book_id"] == books["13 Sonetos"].id
        assert epub["calibre_book_id"] == books["13 Sonetos"].calibre_id
        twin = meta("13 Sonetos.22678.pdf")
        assert twin["book_id"] == books["13 Sonetos"].id
        assert twin["book_twin_of"] == f"{GUTENBERG}/13 Sonetos.22678.epub"
        assert "book_import_error" in meta("Estragado.1001.epub")
        assert "book_id" not in meta("Estragado.1001.epub")


def test_repetir_nao_duplica_nada(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})
    import_zim_books(services, GUTENBERG)
    entered = len(library(services).added)

    again = import_zim_books(services, GUTENBERG)

    assert not again.added and not again.existing and not again.duplicates
    assert again.skipped == 3
    assert len(again.failures) == 2  # as falhas voltam a tentar-se (e continuam a falhar)
    assert len(library(services).added) == entered
    with db() as session:
        assert len(session.scalars(select(models.Book)).all()) == 3


def test_livro_ja_na_biblioteca_nao_se_duplica_mesmo_sem_marca(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})
    import_zim_books(services, GUTENBERG)
    with db() as session:  # perde-se o estado: o sha256 do ficheiro continua a reconhecê-lo
        session.execute(text("UPDATE document SET meta = meta - 'book_id'"))
        session.commit()

    again = import_zim_books(services, GUTENBERG)

    assert not again.added
    assert sorted(b.title for b in again.existing) == ["13 Sonetos", "Outro Livro", "So em PDF"]
    assert len(library(services).books) == 3
    with db() as session:
        assert len(session.scalars(select(models.Book)).all()) == 3


def test_dry_run_nao_escreve_nada(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})

    report = import_zim_books(services, GUTENBERG, dry_run=True)

    assert report.dry_run and report.books == 5 and len(report.would_add) == 5
    assert not report.added and not report.failures
    assert library(services).added == []
    with db() as session:
        assert session.scalars(select(models.Book)).all() == []
        assert (
            session.scalars(
                select(models.Document).where(models.Document.source_id == GUTENBERG)
            ).all()
            == []
        )


def test_limit_conta_livros_e_a_segunda_corrida_continua(
    make_services,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})

    first = import_zim_books(services, GUTENBERG, limit=2)
    assert len(first.added) + len(first.failures) == 2
    assert first.pending == 3

    second = import_zim_books(services, GUTENBERG, limit=10)
    third = import_zim_books(services, GUTENBERG)

    assert len(first.added) + len(second.added) == 3  # nada repetido, nada perdido
    assert not third.added and third.skipped == 3


def test_se_o_epub_falha_entra_o_pdf_do_mesmo_livro(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services(
        {
            "gutenberg_pt_teste": {
                "Livro.5.epub": (EPUB, b"estragado"),
                "Livro.5.pdf": (PDF, pdf_bytes(tmp_path, "Livro")),
            }
        }
    )

    report = import_zim_books(services, GUTENBERG)

    assert not report.failures
    assert [b.format for b in report.added] == ["pdf"]
    assert any("EPUB" in warning for warning in report.warnings)
    with db() as session:
        assert session.scalars(select(models.Book.title)).all() == ["Livro"]


def test_pdf_dentro_de_zim_de_artigos_so_pelos_documentos_marcados(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services(
        {
            "wikipedia_pt_teste": {
                "docs/Manual do Utilizador.pdf": (PDF, pdf_bytes(tmp_path, "Manual")),
                "docs/Outro.pdf": (PDF, pdf_bytes(tmp_path, "Outro")),
            }
        }
    )
    source = services.registry.get(WIKIPEDIA)
    assert isinstance(source, ZimSource)
    with db() as session:  # o importador só registou um dos dois PDF
        found = register_book_candidate(
            session,
            source,
            "docs/Manual do Utilizador.pdf",
            NotAnArticle("", path="docs/Manual do Utilizador.pdf", mimetype=PDF, title=""),
        )
        session.commit()
    assert found is not None

    report = import_zim_books(services, WIKIPEDIA, license_name="CC BY-SA 4.0")

    assert report.files == 1
    assert [b.title for b in report.added] == ["Manual do Utilizador"]
    with db() as session:
        book = session.scalars(select(models.Book)).one()
        assert book.license_name == "CC BY-SA 4.0" and book.license_class == "open"
        row = session.get(models.Document, f"{WIKIPEDIA}/docs/Manual do Utilizador.pdf")
        assert row is not None and row.meta["book_id"] == book.id
        assert row.meta["route"] == "book"


def test_fontes_que_nao_servem(make_services) -> None:  # type: ignore[no-untyped-def]
    services: Services = make_services({"gutenberg_pt_teste": {}})
    with pytest.raises(ZimImportError, match="Unknown source"):
        import_zim_books(services, "zim:nao_existe")
    with pytest.raises(ZimImportError, match=r"is not a ZIM|Unknown source"):
        import_zim_books(services, "course:nada")


# --- linha de comandos ---------------------------------------------------------------------------


def test_cli_import_zim_imprime_o_relatorio_e_lembra_o_kavita(
    make_services,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})
    monkeypatch.setattr(cli_books, "_services", lambda: services)
    monkeypatch.setattr(services, "close", lambda: None)

    result = CliRunner().invoke(cli, ["books", "import-zim", "--source", GUTENBERG, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output and "13 Sonetos.22678.epub" in result.output
    assert library(services).added == []

    result = CliRunner().invoke(cli, ["books", "import-zim", "--source", GUTENBERG])
    assert result.exit_code == 2, result.output  # há falhas por livro
    out = result.output
    assert "added: 3" in out and "already existing: 0" in out
    assert "duplicates ignored (other format): 1" in out
    assert "failures: 2" in out and "Estragado.1001.epub" in out
    assert "ariadne kavita scan" in out  # sem Kavita configurado: avisa, não falha

    result = CliRunner().invoke(cli, ["books", "import-zim", "--source", "zim:nao_existe"])
    assert result.exit_code == 1 and "Unknown source" in result.output


def test_uma_fonte_que_passa_a_privada_a_meio_para_o_lote(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})

    def make_private(_line: str) -> None:  # depois do 1.º livro, alguém marca a fonte privada
        with db() as session:
            session.execute(
                text(
                    "UPDATE source SET license_override = 'private', license_class = 'private' "
                    "WHERE id = :id"
                ),
                {"id": GUTENBERG},
            )
            session.commit()

    with pytest.raises(ZimImportAborted, match="became private") as raised:
        import_zim_books(services, GUTENBERG, progress=make_private)

    assert len(raised.value.report.added) == 1  # só o que entrou antes da marca
    assert len(library(services).added) == 1
    with db() as session:
        assert len(session.scalars(select(models.Book)).all()) == 1


def test_calibre_em_baixo_aborta_sem_marcar_o_livro_como_falhado(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})
    library(services).fail_add = LibraryUnavailable("O Calibre não responde")

    with pytest.raises(ZimImportAborted, match="Calibre não responde") as raised:
        import_zim_books(services, GUTENBERG)

    assert raised.value.report.failures == []
    with db() as session:
        marked = session.scalars(
            select(models.Document.id).where(models.Document.meta.has_key("book_import_error"))
        ).all()
        assert marked == []  # o Calibre em baixo não é culpa do livro
    library(services).fail_add = None
    again = import_zim_books(services, GUTENBERG)  # retomar: o Calibre voltou
    assert len(again.added) == 3


def test_nome_do_temporario_corta_por_bytes_sem_partir_caracteres(
    make_services,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    name = "ç" * 150  # 300 bytes em UTF-8
    services: Services = make_services(
        {"gutenberg_pt_teste": {f"{name}.7.epub": (EPUB, epub_bytes(tmp_path, "X", "x"))}}
    )
    zim = services.registry.get(GUTENBERG)
    assert isinstance(zim, ZimSource)
    path = f"{name}.7.epub"
    candidate = Candidate(path, f"{GUTENBERG}/{path}", "epub", "x", "g", "7", 1)
    scratch = tmp_path / "tmp"
    scratch.mkdir()

    file = _write_temp(scratch, zim, candidate)

    assert len(file.stem.encode()) <= 200 and set(file.stem) <= {"ç"}
    assert file.suffix == ".epub" and file.is_file()


def test_entry_infos_so_devolve_as_entradas_aceites(
    make_services,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})
    zim = services.registry.get(GUTENBERG)
    assert isinstance(zim, ZimSource)
    seen: list[str] = []

    def accept(mimetype: str, path: str) -> bool:
        seen.append(path)
        return format_of(mimetype, path) is not None

    paths = sorted(info.path for info in zim.entry_infos(accept))

    assert "capa.jpg" in seen and "capa.jpg" not in paths  # visto, mas sem ler o tamanho
    assert all(path.endswith((".epub", ".pdf")) for path in paths) and len(paths) == 6
    assert len(list(zim.entry_infos())) > len(paths)


def test_cli_paragem_e_erros_de_infraestrutura(
    make_services,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    services: Services = make_services({"gutenberg_pt_teste": gutenberg_resources(tmp_path)})
    monkeypatch.setattr(cli_books, "_services", lambda: services)
    monkeypatch.setattr(services, "close", lambda: None)
    library(services).fail_add = LibraryUnavailable("O Calibre não responde")

    result = CliRunner().invoke(cli, ["books", "import-zim", "--source", GUTENBERG])
    assert result.exit_code == 1 and "STOPPED" in result.output
    assert "Calibre não responde" in result.output

    def down(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(cli_books, "import_zim_books", down)
    result = CliRunner().invoke(cli, ["books", "import-zim", "--source", GUTENBERG])
    assert result.exit_code == 1
    assert "Error (OperationalError)" in result.output and "Traceback" not in result.output


# --- todas as línguas, sem repetir a obra (ARIAD-32) -----------------------------------------

MUL = "zim:gutenberg_mul_teste"


def lang_epub(
    tmp_path: Path, title: str, topic: str, *, author: str, language: str, **kwargs: object
) -> tuple[str, bytes]:
    file = make_epub(
        tmp_path / "tmp.epub",
        chapters(topic),
        title=title,
        author=author,
        language=language,
        **kwargs,  # type: ignore[arg-type]
    )
    return EPUB, file.read_bytes()


def mul_services(make_services, tmp_path: Path, resources: dict[str, tuple[str, bytes]]):  # type: ignore[no-untyped-def]
    return make_services({"gutenberg_mul_teste": resources})


def test_outra_edicao_da_mesma_obra_na_mesma_lingua_e_recusada(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    eca = "Queirós, Eça de, 1845-1900"
    services = mul_services(
        make_services,
        tmp_path,
        {
            "Os Maias.1.epub": lang_epub(tmp_path, "Os Maias", "Maias", author=eca, language="pt"),
            # outra edição: título com artigo diferente e acentos, o autor na ordem normal
            "Maias.2.epub": lang_epub(
                tmp_path, "OS MAIAS", "Maias outra", author="Eça de Queirós", language="pt"
            ),
            # o mesmo título noutra língua não é a mesma ranhura: entra
            "Os Maias.3.epub": lang_epub(
                tmp_path, "Os Maias", "Maias fr", author=eca, language="fr"
            ),
            # outro autor com o mesmo título: outra obra
            "Os Maias.4.epub": lang_epub(
                tmp_path, "Os Maias", "Maias de outro", author="Outra Pessoa", language="pt"
            ),
        },
    )

    report = import_zim_books(services, MUL)

    assert sorted(b.title for b in report.added) == ["Os Maias", "Os Maias", "Os Maias"]
    (refused,) = report.refused
    assert refused.path == "Maias.2.epub" and refused.language == "pt"
    assert "another edition of the same work in pt" in refused.reason
    assert refused.work.startswith("ta:") and not report.failures
    assert len(library(services).added) == 3  # a recusada nunca chegou ao Calibre
    with db() as session:
        languages = sorted(session.scalars(select(models.Book.language)).all())
        assert languages == ["fr", "pt", "pt"]
        stored = session.scalars(select(models.Book.meta["work"]["language"].astext)).all()
        assert sorted(stored) == ["fr", "pt", "pt"]
        row = session.get(models.Document, f"{MUL}/Maias.2.epub")
        assert row is not None and "book_refused" in row.meta and "book_id" not in row.meta

    again = import_zim_books(services, MUL)  # não se volta a tentar

    assert not again.added and not again.refused and again.refused_before == 1
    forced = import_zim_books(services, MUL, retry_refused=True)
    assert [r.path for r in forced.refused] == ["Maias.2.epub"]  # continua a ser recusada


def test_traducao_so_entra_se_a_obra_ainda_nao_existe_nessa_lingua(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    jules = "Verne, Jules"
    qid = "Q184227"  # a obra, igual em todas as línguas (aqui no identificador do livro)
    services = mul_services(
        make_services,
        tmp_path,
        {
            # o original em francês e uma tradução para inglês: línguas livres, entram
            "A.1.epub": lang_epub(
                tmp_path, "Vingt mille lieues", "fr", author=jules, language="fr", wikidata=qid
            ),
            "B.2.epub": lang_epub(
                tmp_path,
                "Twenty Thousand Leagues",
                "en1",
                author=jules,
                language="en",
                wikidata=qid,
                translator="Mercier Lewis",
            ),
            # outra tradução para inglês: a ranhura está ocupada
            "C.3.epub": lang_epub(
                tmp_path,
                "20,000 Leagues Under the Sea",
                "en2",
                author=jules,
                language="en",
                wikidata=qid,
                translator="F. P. Walter",
            ),
            # uma tradução para francês: a obra já existe em francês
            "D.4.epub": lang_epub(
                tmp_path,
                "Vingt mille lieues (retraduit)",
                "fr2",
                author=jules,
                language="fr",
                wikidata=qid,
                translator="Un Tradutor",
            ),
            # uma tradução para português: livre
            "E.5.epub": lang_epub(
                tmp_path,
                "Vinte mil léguas submarinas",
                "pt",
                author=jules,
                language="pt",
                wikidata=qid,
                translator="Um Tradutor",
            ),
        },
    )

    report = import_zim_books(services, MUL)

    assert sorted(b.title for b in report.added) == [
        "Twenty Thousand Leagues",
        "Vingt mille lieues",
        "Vinte mil léguas submarinas",
    ]
    refused = {r.path: r for r in report.refused}
    assert set(refused) == {"C.3.epub", "D.4.epub"}
    assert refused["C.3.epub"].reason.startswith("translation into en")
    assert refused["D.4.epub"].work == f"wd:{qid}"
    with db() as session:
        work = (
            session.scalars(select(models.Book).where(models.Book.language == "en"))
            .one()
            .meta["work"]
        )
        assert work["key"] == f"wd:{qid}" and work["basis"] == "wikidata" and work["rank"] == 0


def test_o_original_entra_mesmo_que_ja_haja_uma_traducao_e_ela_fica_marcada(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    qid = "Q4242"
    with db() as session:  # a língua original vem do conceito (importado dos sitelinks)
        session.add(models.Concept(wikidata_qid=qid, origin_lang="pt", label="Os Maias"))
        session.commit()
    services = mul_services(
        make_services,
        tmp_path,
        {
            # a tradução para português (de um original noutra língua) entra primeiro
            "A.1.epub": lang_epub(
                tmp_path,
                "Os Maias (trad.)",
                "trad",
                author="Eça de Queirós",
                language="pt",
                wikidata=qid,
                translator="Um Tradutor",
            ),
            # o original em português: entra na mesma
            "B.2.epub": lang_epub(
                tmp_path, "Os Maias", "orig", author="Eça de Queirós", language="pt", wikidata=qid
            ),
            # uma segunda edição do original: recusada
            "C.3.epub": lang_epub(
                tmp_path, "Os Maias", "orig2", author="Eça de Queirós", language="pt", wikidata=qid
            ),
        },
    )

    report = import_zim_books(services, MUL)

    assert len(report.added) == 2
    (superseded,) = report.superseded
    assert superseded.language == "pt"
    (refused,) = report.refused
    assert refused.path == "C.3.epub" and "another edition" in refused.reason
    with db() as session:
        old = session.get(models.Book, superseded.book_id)
        new = session.get(models.Book, superseded.by)
        assert old is not None and new is not None
        assert old.meta["work_superseded_by"] == new.id and new.meta["work"]["rank"] == 2


def test_sem_autor_ou_sem_metadados_o_livro_entra_sempre(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services = mul_services(
        make_services,
        tmp_path,
        {
            "Poemas.1.epub": lang_epub(tmp_path, "Poemas", "um", author="Anonymous", language="pt"),
            "Poemas.2.epub": lang_epub(
                tmp_path, "Poemas", "dois", author="Anonymous", language="pt"
            ),
            "Manual.3.pdf": (PDF, pdf_bytes(tmp_path, "Manual")),
        },
    )

    report = import_zim_books(services, MUL)

    assert len(report.added) == 3 and not report.refused


def test_dry_run_diz_o_que_a_regra_recusaria_sem_escrever_nada(
    make_services,
    tmp_path: Path,
    db: sessionmaker[Session],  # type: ignore[no-untyped-def]
) -> None:
    services = mul_services(
        make_services,
        tmp_path,
        {
            "A.1.epub": lang_epub(tmp_path, "Dom Casmurro", "um", author="Machado", language="pt"),
            "B.2.epub": lang_epub(
                tmp_path, "Dom Casmurro", "dois", author="Machado", language="pt"
            ),
        },
    )

    report = import_zim_books(services, MUL, dry_run=True)

    assert [b.path for b in report.would_add] == ["A.1.epub"]
    assert [r.path for r in report.refused] == ["B.2.epub"]
    with db() as session:
        assert not session.scalars(select(models.Book)).all()
        row = session.get(models.Document, f"{MUL}/B.2.epub")
        assert row is None or "book_refused" not in row.meta  # o dry-run não regista nada


def test_o_relatorio_do_comando_conta_as_recusadas(capsys: pytest.CaptureFixture[str]) -> None:
    report = ZimImportReport(MUL)
    report.refused.append(Refused("B.epub", "Dom Casmurro", "pt", "ta:x|y", "outra edição", "dom"))
    report.refused_before = 2
    report.superseded.append(Superseded("velho", "novo", "pt"))

    cli_books._print_zim_report(report)

    text = capsys.readouterr().out
    assert "refused by the language rule: 1" in text
    assert "refused before: 2" in text and "superseded by an original: 1" in text
    assert "Dom Casmurro" in text and "velho" in text
