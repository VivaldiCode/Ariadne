"""Remover e arquivar ZIM (Fase 12): `DELETE /api/sources/{id}`.

Remover é **arquivar**: o ficheiro passa para `ZIM_DIR/.archive/` (o kiwix e o registo deixam de o
ver; os documentos e o índice da fonte ficam, inativos) e pode voltar com `restore`. Só se apaga o
ficheiro com confirmação explícita (o id da fonte repetido), e nesse caso apaga-se mesmo do disco.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ariadne.db import models
from ariadne.documents import LibraryError
from ariadne.services import Services

ARCHIVE_DIR = ".archive"


class SourceNotRemovable(LibraryError):
    """A fonte não é um ZIM da pasta (curso, livro) ou o ficheiro não está onde devia."""


@dataclass
class RemovalResult:
    source_id: str
    action: str  # archived | deleted
    file: str | None


def _zim_row(session: Session, source_id: str) -> models.Source:
    row = session.get(models.Source, source_id)
    if row is None:
        raise SourceNotRemovable(f"Source {source_id!r} does not exist.")
    if row.kind != "zim":
        raise SourceNotRemovable(
            f"{source_id} is not a ZIM ({row.kind}): courses and books are removed in their "
            "own modules."
        )
    return row


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def remove_zim(
    services: Services, session: Session, source_id: str, *, delete_file: bool, confirm: str | None
) -> RemovalResult:
    row = _zim_row(session, source_id)
    zim_dir = services.settings.zim_dir
    archive = zim_dir / ARCHIVE_DIR
    if delete_file and confirm != source_id:
        raise SourceNotRemovable(
            "Deleting the file requires explicit confirmation: repeat the source id in `confirm`."
        )
    path = Path(row.path) if row.path else None
    archived_path = archive / path.name if path else None
    if path is None or not (path.is_file() or (archived_path and archived_path.is_file())):
        raise SourceNotRemovable(f"The file of {source_id} no longer exists in {zim_dir}.")
    if path.is_file() and not _inside(path, zim_dir):
        raise SourceNotRemovable("The file is outside ZIM_DIR: not touching it.")
    if delete_file:
        for candidate in (path, archived_path):
            if candidate is not None and candidate.is_file():
                candidate.unlink()
        action = "deleted"
        file = path.name
    else:
        if path.is_file():
            archive.mkdir(exist_ok=True)
            assert archived_path is not None
            if archived_path.exists():
                raise SourceNotRemovable(f"{archived_path.name} already exists in the archive.")
            shutil.move(str(path), str(archived_path))
        action = "archived"
        file = path.name
    services.registry.sync(session)  # a fonte sai das ativas (fica «missing»)…
    session.execute(  # …e passa a «archived» (o registo não a volta a tocar)
        update(models.Source).where(models.Source.id == source_id).values(status="archived")
    )
    session.commit()
    return RemovalResult(source_id, action, file)


def restore_zim(services: Services, session: Session, source_id: str) -> RemovalResult:
    row = _zim_row(session, source_id)
    if row.status != "archived" or not row.path:
        raise SourceNotRemovable(f"{source_id} is not archived.")
    zim_dir = services.settings.zim_dir
    name = Path(row.path).name
    source = zim_dir / ARCHIVE_DIR / name
    target = zim_dir / name
    if not source.is_file():
        raise SourceNotRemovable(f"The archived file {name} no longer exists.")
    if target.exists():
        raise SourceNotRemovable(f"{name} already exists in ZIM_DIR.")
    os.replace(source, target)
    services.registry.sync(session)
    return RemovalResult(source_id, "restored", name)


def installed_index(session: Session) -> list[models.Source]:
    return list(
        session.scalars(
            select(models.Source).where(
                models.Source.kind == "zim", models.Source.status.in_(("active", "archived"))
            )
        )
    )
