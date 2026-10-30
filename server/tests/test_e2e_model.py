"""Ponta a ponta com o modelo real (e5-small int8 em ONNX), o kiwix e o ZIM de teste do compose.

Prova o critério da Fase 3: a mesma pergunta duas vezes. A 1.ª é servida pelo nível 1 e enfileira os
documentos; o indexador vetoriza-os; a 2.ª já sai do nível 3. O modelo (118 MB) descarrega-se na
1.ª vez para `server/.cache/models` (ignorada pelo git); `-m 'not model'` salta este teste.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, create_principal
from ariadne.config import Settings
from ariadne.db.models import ChunkVector, Job, QueryLog
from ariadne.search import search
from ariadne.services import Services
from ariadne.workers.runner import Runner

pytestmark = [pytest.mark.model, pytest.mark.db, pytest.mark.kiwix]

CACHE = Path(__file__).resolve().parents[1] / ".cache" / "models"
QUESTION = "When did Ray Charles go blind?"


@pytest.fixture
def services(database_url: str, kiwix_url: str, real_zim: Path) -> Iterator[Services]:
    settings = Settings(
        database_url=database_url,
        zim_dir=real_zim.parent,
        kiwix_url=kiwix_url,
        kiwix_public_url=kiwix_url,
        embedder="onnx",
        embedder_variant="int8",
        models_dir=CACHE,
    )
    services = Services.open(settings)
    services.sync_sources()
    yield services
    services.close()


def test_pergunta_repetida_passa_a_ser_servida_pelo_nivel_3(
    services: Services, db: sessionmaker[Session]
) -> None:
    model_id = services.get_embedder().model_id
    assert model_id == "intfloat/multilingual-e5-small@614241f:int8"
    with db() as session, session.begin():
        principal, _ = create_principal(session, PrincipalKind.agent, "e2e")
        principal_id = principal.id

    with db() as session:
        first = search(services, session, principal_id, QUESTION, k=5)
        assert first.results and {h.level for h in first.results} == {1}
        queued = {j.payload["document_id"] for j in session.scalars(select(Job))}
        assert queued == {h.document_id for h in first.results}

        runner = Runner(services, ["indexer"], worker_id="e2e")
        for _ in range(len(queued) + 5):
            if not runner.run_once():
                break
        session.expire_all()
        assert {j.status for j in session.scalars(select(Job))} == {"done"}
        vectors = session.scalar(
            select(func.count()).select_from(ChunkVector).where(ChunkVector.model_id == model_id)
        )
        assert vectors and vectors > 0
        assert services.store.indexed(session, sorted(queued), model_id) == queued

        second = search(services, session, principal_id, QUESTION, k=5)
        log = session.get_one(QueryLog, second.query_id)

    assert second.results[0].level == 3 and second.results[0].chunk_id is not None
    assert log.level_served == 3
    assert second.results[0].document_id in queued
    assert not [w for w in second.warnings if "indisponível" in w]
