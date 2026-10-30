"""Pôr documentos de volta na fila do `linker` (`document.linked_at IS NULL`).

Quem muda o que o linker usa (título, títulos alternativos, vetor de artigo) usa isto: apaga
também a concessão (`link_lease`), para o linker que estava a meio não dar o documento por visto
com os dados antigos, e as tentativas falhadas.
"""

from typing import Any

from sqlalchemy import ColumnElement, case

from ariadne.db.models import Document as DocumentRow

RELINK: dict[str, Any] = {"linked_at": None, "link_lease": None, "link_attempts": 0}


def relink_when(condition: ColumnElement[bool]) -> dict[str, Any]:
    """Os valores de um `ON CONFLICT DO UPDATE`: repõe na fila só se `condition`."""
    return {
        "linked_at": case((condition, None), else_=DocumentRow.linked_at),
        "link_lease": case((condition, None), else_=DocumentRow.link_lease),
        "link_attempts": case((condition, 0), else_=DocumentRow.link_attempts),
    }
