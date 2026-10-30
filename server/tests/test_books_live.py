"""Calibre e Kavita de verdade (os do compose): `docker compose up -d calibre kavita`.

Cada teste usa um livro com título único e retira-o do Calibre no fim. Marcas: `calibre` e `kavita`.
"""

import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, create_principal
from ariadne.books.add import add_book
from ariadne.books.backend import LibraryBook
from ariadne.books.calibre import CalibreBackend
from ariadne.books.extract import extract_book
from ariadne.books.kavita import KavitaClient, sync_locations
from ariadne.config import Settings
from ariadne.db import models
from ariadne.embedder import FakeEmbedder
from ariadne.licenses import LicenseClass
from ariadne.search import search
from ariadne.services import Services
from ariadne.sources.registry import SourceRegistry
from tests.bookfactory import chapter_body, make_epub, make_pdf
from tests.fakes import default_fake


@pytest.fixture
def calibre(calibre_access: tuple[str, str, Path]) -> Iterator[CalibreBackend]:
    url, key, _ = calibre_access
    backend = CalibreBackend(url, api_key=key, timeout=300)
    yield backend
    backend.close()


@pytest.fixture
def title() -> str:
    return f"Livro de teste {uuid.uuid4().hex[:8]}"


@pytest.fixture
def epub(tmp_path: Path, title: str) -> Path:
    chapters = [
        ("Primeira parte", chapter_body("Primeira parte", "O rio Tejo")),
        ("Segunda parte", chapter_body("Segunda parte", "A serra da Estrela")),
    ]
    return make_epub(tmp_path / "livro.epub", chapters, title=title)


@pytest.mark.calibre
def test_calibre_adiciona_le_metadados_e_remove(
    calibre: CalibreBackend, calibre_access: tuple[str, str, Path], epub: Path, title: str
) -> None:
    root = calibre_access[2]
    assert "calibre" in calibre.check()
    book: LibraryBook | None = None
    try:
        book = calibre.add(epub)

        assert book.title == title and book.authors == ("Ana Autora",)
        relative = book.path_of("EPUB")
        assert relative and (root / relative).is_file()  # o ficheiro está no volume partilhado
        assert (root / relative).read_bytes() == epub.read_bytes()  # o Calibre não o altera
        assert calibre.get(book.id).title == title
        assert any(b.id == book.id for b in calibre.list_books())
        assert [c.title for c in extract_book(root / relative).chapters] == [
            "Primeira parte",
            "Segunda parte",
        ]
    finally:
        if book is not None:
            calibre.remove(book.id)
    assert not (root / (relative or "x")).exists()


@pytest.mark.calibre
def test_calibre_adiciona_pdf_e_converte_epub_noutro_formato(
    calibre: CalibreBackend, tmp_path: Path
) -> None:
    pages = [[f"Linha {n} sobre o rio Tejo e a cidade."] * 6 for n in range(1, 5)]
    pdf = make_pdf(tmp_path / "livro.pdf", pages, title=f"PDF {uuid.uuid4().hex[:8]}")
    book = calibre.add(pdf)
    try:
        assert set(book.formats) == {"PDF"}
    finally:
        calibre.remove(book.id)


@pytest.mark.calibre
def test_calibre_recusa_a_chave_errada(calibre_access: tuple[str, str, Path]) -> None:
    from ariadne.books.backend import LibraryUnavailable

    bad = CalibreBackend(calibre_access[0], api_key="errada")
    with pytest.raises(LibraryUnavailable, match="CALIBRE_API_KEY"):
        bad.list_books()
    bad.close()


@pytest.mark.kavita
def test_do_calibre_ao_kavita_com_url_do_leitor(
    db: sessionmaker[Session],
    tmp_path: Path,
    calibre_access: tuple[str, str, Path],
    kavita_access: tuple[str, str, int],
    epub: Path,
    title: str,
) -> None:
    calibre_url, calibre_key, root = calibre_access
    kavita_url, kavita_key, library_id = kavita_access
    settings = Settings(
        database_url="postgresql+psycopg://x@localhost/x",
        zim_dir=tmp_path / "zim",
        books_dir=root,
        calibre_url=calibre_url,
        calibre_api_key=calibre_key,
        kavita_url=kavita_url,
        kavita_api_key=kavita_key,
        kavita_library_id=library_id,
        kavita_public_url="http://kavita.publico:8806",
        kiwix_url="http://kiwix",
    )
    registry = SourceRegistry.from_settings(settings)
    services = Services(settings, db, registry, default_fake().client(), embedder=FakeEmbedder())
    client = KavitaClient(kavita_url, kavita_key)
    calibre_id: int | None = None
    try:
        added = add_book(
            services,
            epub,
            license_class=LicenseClass.OPEN,
            license_name="Domínio público",
            book_id=f"teste-{uuid.uuid4().hex[:8]}",
        )
        calibre_id = added.calibre_id
        # O Kavita lê a biblioteca (assíncrono): espera até o livro aparecer.
        client.scan(library_id, force=True)
        deadline = time.monotonic() + 90
        while True:
            with db() as session:
                found = sync_locations(session, client, library_id, "/books")
                book = session.get(models.Book, added.book_id)
                assert book is not None
                if book.kavita_series_id or time.monotonic() > deadline:
                    break
            time.sleep(3)
        assert book.kavita_series_id, "o Kavita não leu o livro (enableMetadata na biblioteca?)"
        assert book.kavita_chapter_id
        with db() as session, session.begin():
            principal, _ = create_principal(session, PrincipalKind.agent, "agente")
            principal_id = principal.id
        with db() as session:
            result = search(
                services, session, principal_id, "serra da Estrela",
                sources=[f"book:{added.book_id}"],
            )  # fmt: skip
        [hit] = [h for h in result.results if h.title == "Segunda parte"]
        assert hit.reader_url == (
            f"http://kavita.publico:8806/library/{library_id}/series/{book.kavita_series_id}"
            f"/book/{book.kavita_chapter_id}"
        )
        assert found.mapped >= 0
    finally:
        if calibre_id is not None:
            services.get_library().remove(calibre_id)
        client.scan(library_id, force=True)  # o livro de teste sai do Kavita
        client.close()
        services.close()
