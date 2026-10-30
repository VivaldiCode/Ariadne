"""Catálogo das fontes disponíveis (a ferramenta `sources` e `GET /api/sources`)."""

import os

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ariadne.rooms import WingRooms, rooms_by_wing
from ariadne.services import Services


class SourceInfo(BaseModel):
    id: str
    title: str
    kind: str
    language: str | None
    version: str
    articles: int
    wing: str
    # A classificação à entrada (artigo, curso ou livro) e o que os documentos da fonte são.
    content_class: str = "article"
    content_type: str = "article"
    archive_only: bool = False  # só serve de arquivo (o conteúdo vem do curso extraído)
    license_class: str = "open"  # `open` ou `private` (STRUCTURE.md, secção 7)
    size_bytes: int | None = None  # espaço que a fonte ocupa em disco (o ficheiro ZIM)
    # As salas da ala (temas, calculados dos vetores de artigo): quantas e as maiores. Os ids
    # servem de filtro `rooms` na pesquisa. Vazio enquanto a ala não tem vetores de artigo.
    rooms: WingRooms = WingRooms(count=0, top=[])


def _size(path: str | None) -> int | None:
    try:
        return os.stat(path).st_size if path else None
    except OSError:
        return None


def list_sources(services: Services, session: Session) -> list[SourceInfo]:
    services.registry.sync_if_changed(session)
    active = services.registry.active()
    rooms = rooms_by_wing(session, sorted({source.wing for source in active}))
    return [
        SourceInfo(
            id=source.id,
            title=source.title,
            kind=source.kind,
            language=source.language,
            version=source.version,
            articles=source.article_count(),
            wing=source.wing,
            content_class=str(source.meta.get("content_class", "article")),
            content_type=source.content_type,
            archive_only=bool(source.meta.get("archive_only", False)),
            license_class=source.license_class.value,
            size_bytes=_size(source.path),
            rooms=rooms.get(source.wing, WingRooms(count=0, top=[])),
        )
        for source in active
    ]
