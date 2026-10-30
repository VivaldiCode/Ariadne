"""Migrações Alembic chamadas por código (CLI `ariadne migrate` e testes)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # O ConfigParser do Alembic interpola '%'; uma palavra-passe com '%' partia o URL.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(database_url), revision)


def head_revision() -> str:
    """A revisão mais recente das migrações (os testes não a repetem à mão)."""
    head = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
    assert head is not None
    return head
