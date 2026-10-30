"""A linha de comandos dos livros e das licenças, e a recusa das exportações de vetores."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ariadne import cli_books
from ariadne.cli import app as cli
from ariadne.config import Settings, get_settings
from ariadne.db import models
from ariadne.embedder import FakeEmbedder
from ariadne.services import Services
from ariadne.sources.registry import SourceRegistry
from tests.bookfactory import chapter_body, make_epub, make_pdf
from tests.bookfakes import FakeLibrary
from tests.fakes import default_fake
from tests.zimfactory import make_zim

pytestmark = pytest.mark.db

CHAPTERS = [
    ("Um", chapter_body("Um", "Primeiro assunto")),
    ("Dois", chapter_body("Dois", "Segundo assunto")),
]


@pytest.fixture
def env(
    database_url: str,
    db: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    zims = tmp_path / "zim"
    zims.mkdir()
    make_zim(zims / "wikipedia_pt_teste_2026-09.zim")
    root = tmp_path / "biblioteca"
    root.mkdir()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ZIM_DIR", str(zims))
    monkeypatch.setenv("BOOKS_DIR", str(root))
    get_settings.cache_clear()

    def open_services() -> Services:
        settings = Settings(
            database_url=database_url, zim_dir=zims, books_dir=root, kiwix_url="http://kiwix"
        )
        services = Services(
            settings,
            db,
            SourceRegistry.from_settings(settings),
            default_fake().client(),
            embedder=FakeEmbedder(),
        )
        services.library = FakeLibrary(root)
        return services

    monkeypatch.setattr(cli_books, "_services", open_services)
    yield tmp_path
    get_settings.cache_clear()


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def test_add_exige_a_classe_e_a_licenca(env: Path) -> None:
    file = make_epub(env / "livro.epub", CHAPTERS)

    code, out = run("books", "add", str(file), "--license-name", "PD")
    assert code == 2 and "--license-class" in out

    code, out = run("books", "add", str(file), "--license-class", "open")
    assert code == 2 and "--license-name" in out

    code, out = run("books", "add", str(file), "--license-class", "talvez", "--license-name", "PD")
    assert code == 2 and "nvalid" in out


def test_add_recusa_drm_com_mensagem_clara(env: Path) -> None:
    adept = make_epub(env / "adept.epub", CHAPTERS, drm="adept")
    pdf = make_pdf(env / "cifrado.pdf", [["texto"] * 5] * 2, encrypted=True)

    for file in (adept, pdf):
        code, out = run(
            "books", "add", str(file), "--license-class", "private", "--license-name", "Comprado"
        )
        assert code == 1
        assert "DRM" in out or "Encrypted" in out
        assert "does not accept" in out or "does not circumvent" in out


def test_ciclo_add_list_show_set_license(env: Path, db: sessionmaker[Session]) -> None:
    file = make_epub(env / "livro.epub", CHAPTERS, title="Livro CLI")

    code, out = run(
        "books", "add", str(file), "--license-class", "private", "--license-name", "Comprado",
        "--id", "livro-cli",
    )  # fmt: skip
    assert code == 0, out
    assert "livro-cli" in out and "2 chapters" in out and "class private" in out

    code, out = run("books", "add", str(file), "--license-class", "private", "--license-name", "x")
    assert code == 0 and "was already in the library" in out

    code, out = run("books", "list")
    assert "livro-cli" in out and "private" in out and "not in Kavita" in out
    code, out = run("books", "show", "livro-cli")
    assert "Ana Autora" in out and "Comprado" in out and "1. Um" in out
    assert run("books", "show", "nao-existe")[0] == 1

    code, out = run("books", "set-license", "livro-cli", "open")
    assert code == 2 and "--yes" in out
    code, out = run("books", "set-license", "livro-cli", "open", "--yes")
    assert code == 0 and "2 documents changed" in out
    with db() as session:
        assert session.get(models.Source, "book:livro-cli").license_class == "open"  # type: ignore[union-attr]
        assert session.get(models.Book, "livro-cli").license_class == "open"  # type: ignore[union-attr]
    code, out = run("books", "set-license", "livro-cli", "private")
    assert code == 0 and "2 documents changed" in out

    code, out = run("licenses", "list")
    assert "book:livro-cli  private  2 private documents" in out
    assert "zim:wikipedia_pt_teste  open  0 private documents" in out


def test_index_pede_os_vetores_dos_capitulos(env: Path, db: sessionmaker[Session]) -> None:
    file = make_epub(env / "livro.epub", CHAPTERS)
    run("books", "add", str(file), "--license-class", "open", "--license-name", "PD", "--id", "l")

    code, out = run("books", "index", "l")

    assert code == 0 and "2 chapters in the indexing queue" in out
    code, out = run("books", "sync", "--force")
    assert code == 0 and "1 books extracted, 2 chapters" in out


def test_exportar_vetores_de_um_livro_privado_e_recusado(env: Path) -> None:
    file = make_epub(env / "livro.epub", CHAPTERS)
    run("books", "add", str(file), "--license-class", "private", "--license-name", "x", "--id", "p")
    out_file = env / "vetores.parquet"

    code, out = run("vectors", "build", "--source", "book:p", "--out", str(out_file))

    assert code == 1 and "blocked" in out and "book:p" in out
    assert not out_file.exists()
    code, out = run("licenses", "blocks")
    assert "vectors build --out: blocked" in out


def test_exportar_vetores_de_uma_fonte_aberta_continua_a_funcionar(env: Path) -> None:
    out_file = env / "vetores.parquet"

    code, out = run(
        "vectors", "build", "--source", "zim:wikipedia_pt_teste", "--out", str(out_file)
    )

    assert code == 0, out
    assert out_file.exists()


def test_marcar_um_zim_privado_pela_cli(env: Path) -> None:
    assert run("sources", "sync")[0] == 0  # regista as fontes
    code, out = run("licenses", "mark-private", "zim:wikipedia_pt_teste")
    assert code == 0 and "persistent mark" in out
    assert "zim:wikipedia_pt_teste  private" in run("licenses", "list")[1]
    assert run("licenses", "mark-private", "zim:nao-existe")[0] == 1

    code, out = run("licenses", "clear-mark", "zim:wikipedia_pt_teste")
    assert code == 2 and "--yes" in out
    assert run("licenses", "clear-mark", "zim:wikipedia_pt_teste", "--yes")[0] == 0
    assert "zim:wikipedia_pt_teste  open" in run("licenses", "list")[1]
