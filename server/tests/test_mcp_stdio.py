"""`ariadne mcp-stdio`: o mesmo servidor MCP por stdio, com o principal na env ARIADNE_API_KEY."""

import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, create_principal
from ariadne.db.models import ToolCall

pytestmark = pytest.mark.db

ARIADNE = str(Path(sys.executable).parent / "ariadne")


def _env(database_url: str, zim_dir: Path, key: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": database_url,
        "ZIM_DIR": str(zim_dir),
        "KIWIX_URL": "http://localhost:1",
        "ARIADNE_API_KEY": key,
    }


def test_stdio_le_e_regista_a_chamada(
    database_url: str, db: sessionmaker[Session], zim_dir: Path
) -> None:
    with db() as session, session.begin():
        principal, key = create_principal(session, PrincipalKind.agent, "dev-stdio")
        principal_id = principal.id
    params = StdioServerParameters(
        command=ARIADNE, args=["mcp-stdio"], env=_env(database_url, zim_dir, key)
    )

    async def main() -> tuple[list[str], str]:
        async with Client(params) as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
            doc = await client.call_tool("read", {"document_id": "zim:wikipedia_pt_teste/Portugal"})
            assert doc.structured_content is not None
            return tools, doc.structured_content["title"]

    tools, title = anyio.run(main)

    assert sorted(tools) == [
        "feedback",
        "neighbors",
        "read",
        "rooms",
        "search",
        "sources",
        "translate",
        "translations",
    ]
    assert title == "Portugal"
    with db() as session:
        [call] = session.scalars(select(ToolCall)).all()
    assert (call.tool, call.principal_id, call.error) == ("read", principal_id, None)
    assert call.session_id is not None and call.session_id.startswith("stdio-")


@pytest.mark.parametrize(
    ("key", "message"),
    [("", "ARIADNE_API_KEY is missing"), ("errada", "does not match any principal")],
)
def test_stdio_exige_uma_chave_valida(
    database_url: str, zim_dir: Path, key: str, message: str
) -> None:
    done = subprocess.run(
        [ARIADNE, "mcp-stdio"],
        env=_env(database_url, zim_dir, key),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 1
    assert message in done.stderr
    assert done.stdout == ""  # stdout é só do protocolo


def test_stdio_valida_a_chave_a_cada_chamada(db: sessionmaker[Session]) -> None:
    """Uma chave revogada com o processo já a correr deixa de funcionar na chamada seguinte."""
    from typing import Any, cast

    from mcp.server.mcpserver.exceptions import ToolError

    from ariadne.db.models import Principal
    from ariadne.mcp.server import stdio_caller

    with db() as session, session.begin():
        principal, key = create_principal(session, PrincipalKind.agent, "dev-stdio")
        principal_id = principal.id
    resolve = stdio_caller(db, key, "stdio-teste")
    ctx = cast("Any", None)  # o resolvedor de stdio não usa o contexto

    caller = resolve(ctx)
    assert (caller.principal_id, caller.session_id) == (principal_id, "stdio-teste")

    with db() as session, session.begin():
        session.get_one(Principal, principal_id).api_key_hash = None  # revogada
    with pytest.raises(ToolError, match="revoked"):
        resolve(ctx)
