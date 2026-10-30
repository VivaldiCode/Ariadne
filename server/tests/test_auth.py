import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ariadne.app import create_app
from ariadne.auth import KEY_PREFIX, PrincipalKind, create_principal, hash_api_key
from ariadne.config import Settings
from ariadne.db.session import make_engine

pytestmark = pytest.mark.db


def test_whoami_com_e_sem_chave(migrated_database_url: str) -> None:
    engine = make_engine(migrated_database_url)
    with Session(engine) as session, session.begin():
        principal, key = create_principal(session, PrincipalKind.agent, "agente-de-teste")
        stored_hash = principal.api_key_hash
    engine.dispose()

    assert key.startswith(KEY_PREFIX)
    assert stored_hash == hash_api_key(key)

    app = create_app(Settings(database_url=migrated_database_url))
    with TestClient(app) as client:
        ok = client.get("/api/whoami", headers={"Authorization": f"Bearer {key}"})
        wrong = client.get("/api/whoami", headers={"Authorization": "Bearer ariadne_errada"})
        missing = client.get("/api/whoami")

    assert ok.status_code == 200
    assert ok.json()["name"] == "agente-de-teste"
    assert ok.json()["kind"] == "agent"
    assert wrong.status_code == 401
    assert missing.status_code == 401
