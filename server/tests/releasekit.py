"""Fixtures e auxiliares partilhados pelos testes da release (conteúdo, segurança, backups)."""

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ariadne.config import Settings
from ariadne.courses.repo import CourseRepo
from ariadne.db import models
from ariadne.embedder import FakeEmbedder
from ariadne.release import content
from ariadne.services import Services
from ariadne.sources.registry import SourceRegistry
from tests.coursefactory import make_course
from tests.fakes import default_fake

needs_tools = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("ssh-keygen") is None,
    reason="precisa dos binários age e ssh-keygen",
)

OPEN_SOURCE = "zim:aberta_pt"
SECRET_BODY = "SEGREDO-DO-LIVRO-PRIVADO"
SECRET_TITLE = "Título Reservado"
OPEN_BODY = "Texto aberto sobre Lisboa e o Tejo."
USER_NAME = "utilizador-nunca-na-release"


def age_keypair(folder: Path, name: str = "privada.txt") -> tuple[str, Path]:
    """Um par age de teste, FORA do repositório; a privada só aqui, nunca no sistema que cifra."""
    folder.mkdir(parents=True, exist_ok=True)
    if shutil.which("age-keygen") is None:
        pytest.skip("precisa do binário age-keygen")
    out = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    public = re.search(r"(age1[a-z0-9]+)", out.stderr + out.stdout)
    assert public
    identity = folder / name
    identity.write_text(out.stdout)
    identity.chmod(0o600)
    return public.group(1), identity


def signing_keypair(folder: Path, name: str = "assinatura") -> tuple[Path, str]:
    """(ficheiro da chave privada de assinatura, «ssh-ed25519 AAAA…»)."""
    folder.mkdir(parents=True, exist_ok=True)
    key = folder / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "teste", "-f", str(key)],
        check=True,
    )
    return key, " ".join((folder / f"{name}.pub").read_text().split()[:2])


@pytest.fixture
def keys(tmp_path: Path) -> tuple[str, Path]:
    return age_keypair(tmp_path / "chaves")


@pytest.fixture
def signing(tmp_path: Path) -> tuple[Path, str]:
    return signing_keypair(tmp_path / "assinatura")


def add_page(
    session: Session, source_id: str, name: str, body: str, *, private_doc: bool = False
) -> str:
    document_id = f"{source_id}/{name}"
    session.add(
        models.Document(
            id=document_id,
            source_id=source_id,
            path=name,
            version="2026-09",
            title=name.replace("-", " ").title() if not private_doc else SECRET_TITLE,
            language="pt",
            wiki_page_id=abs(hash(document_id)) % 10_000 + 1,
            license_class="private" if private_doc else "open",
            meta={"wiki_path": f"{source_id.split(':')[1]}/pt/{name}"},
        )
    )
    session.flush()
    session.add(
        models.Curation(
            document_id=document_id,
            source_version="2026-09",
            fingerprint=f"fp-{name}",
            decision="published",
            original_md=body,
            published_md=body,
        )
    )
    return document_id


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> None:
    with db() as session, session.begin():
        session.add_all(
            [
                models.Source(
                    id=OPEN_SOURCE,
                    kind="zim",
                    title="Aberta PT",
                    language="pt",
                    version="2026-09",
                    wing="aberta_pt",
                    license_class="open",
                    meta={
                        "zim_name": "aberta_pt",
                        "uuid": "u-1",
                        "origin_source_id": "zim:origem-privada",
                        "path_interno": "/zim/nao-sai.zim",
                    },
                ),
                models.Source(
                    id="book:reservado",
                    kind="book",
                    title="Livro comprado",
                    language="pt",
                    version="1",
                    wing="reservado",
                    license_class="private",
                    meta={},
                ),
                models.Source(
                    id="zim:marcada",
                    kind="zim",
                    title="Marcada à mão",
                    language="pt",
                    version="1",
                    wing="marcada",
                    license_class="private",
                    license_override="private",
                    meta={},
                ),
            ]
        )
        session.flush()
        add_page(
            session,
            OPEN_SOURCE,
            "lisboa",
            f"# Lisboa\n\n{OPEN_BODY}\n\nVer [Porto](ariadne:porto).",
        )
        add_page(session, OPEN_SOURCE, "porto", "# Porto\n\nCidade do norte.")
        add_page(session, OPEN_SOURCE, "reservada", f"# X\n\n{SECRET_BODY}", private_doc=True)
        add_page(session, "book:reservado", "capitulo-1", f"{SECRET_BODY} capítulo 1")
        add_page(session, "zim:marcada", "pagina", f"{SECRET_BODY} marcada")
        session.add(
            models.Principal(
                name=USER_NAME, kind="user", role="admin", api_key_hash="hash-secreto-de-chave"
            )
        )
        session.add(
            models.QueryLog(principal_id=None, query=f"pergunta de {USER_NAME}", level_served=1)
        )


@pytest.fixture
def repo(tmp_path: Path) -> CourseRepo:
    courses = CourseRepo(tmp_path / "cursos")
    courses.save(make_course("curso-aberto"))
    private = make_course("curso-privado")
    private.license.license_class = content.LicenseClass.PRIVATE
    courses.save(private)
    # Um curso aberto com UMA versão privada (a nova): só a aberta pode sair.
    courses.save(make_course("curso-misto", version="2026-09"))
    novo = make_course("curso-misto", version="2026-10", title="Versão reservada")
    novo.license.license_class = content.LicenseClass.PRIVATE
    courses.save(novo)
    return courses


def make_settings(
    database_url: str,
    tmp_path: Path,
    repo: CourseRepo,
    public_key: str,
    signing: tuple[Path, str],
    **extra: Any,
) -> Settings:
    values: dict[str, Any] = {
        "database_url": database_url,
        "zim_dir": tmp_path / "zim",
        "courses_repo": repo.path,
        "backup_dir": tmp_path / "backups",
        "kiwix_url": "http://kiwix",
        "release_age_recipient": public_key,
        "release_part_bytes": 1024,
        "release_signing_key_file": str(signing[0]),
        "release_signing_pubkey": signing[1],
        "wikijs_token": "TOKEN-SECRETO-DO-WIKI",
        "curator_api_key": "CHAVE-SECRETA-DO-CURADOR",
        "wikijs_admin_email": "dono@example.org",
    }
    values.update(extra)
    return Settings(**values)


@pytest.fixture
def services(
    database_url: str,
    db: sessionmaker[Session],
    tmp_path: Path,
    repo: CourseRepo,
    keys: tuple[str, Path],
    signing: tuple[Path, str],
    seeded: None,
) -> Iterator[Services]:
    (tmp_path / "zim").mkdir()
    settings = make_settings(database_url, tmp_path, repo, keys[0], signing)
    services = Services(
        settings,
        db,
        SourceRegistry.from_settings(settings),
        default_fake().client(),
        embedder=FakeEmbedder(),
    )
    yield services
    services.close()


def tree_text(root: Path) -> str:
    return "\n".join(
        f"{p.relative_to(root)}\n{p.read_text(errors='replace')}"
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )
