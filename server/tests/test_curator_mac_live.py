"""Ponta a ponta com o curador de verdade (o llama-server do Mac): um job `curate_page` é
processado pelo Mac, com o URL, o modelo e a chave vindos do cofre.

Só corre com `TEST_CURATOR_URL` (e `TEST_CURATOR_KEY`) no ambiente; sem eles é ignorado:

    TEST_CURATOR_URL=http://<IP do Mac>:8085 TEST_CURATOR_KEY=<a chave do Mac> \\
        uv run pytest tests/test_curator_mac_live.py -s
"""

import os
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ariadne.vault import crypto
from ariadne.vault.admin import VaultAdmin
from ariadne.vault.config import IntegrationConfig
from tests.curationzims import REMEDIO, make_dir
from tests.test_curation import build, curation_of, request_import, run_jobs

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("short_articles"),
    pytest.mark.skipif(not os.environ.get("TEST_CURATOR_URL"), reason="sem TEST_CURATOR_URL"),
]


def test_um_job_de_curadoria_e_processado_pelo_mac(
    db: sessionmaker[Session], tmp_path: Path
) -> None:
    url = os.environ["TEST_CURATOR_URL"]
    services, _wiki = build(db, make_dir(tmp_path / "zims"))  # sem CURATOR_URL no ambiente
    try:
        services.settings.ariadne_master_key = crypto.generate_master_key()
        services.config = IntegrationConfig(services.settings, services.sessions, ttl=0)
        admin = VaultAdmin(services.config)
        assert services.get_curator() is None
        admin.set_value("curator_url", url, "teste")
        admin.set_value("curator_model", "gemma-3-12b-it", "teste")
        admin.set_value("curator_api_key", os.environ.get("TEST_CURATOR_KEY", "sem-chave"), "teste")
        assert services.curator_status(force=True).online is True

        request_import(services, REMEDIO)  # saúde: passa sempre pelo curador
        run_jobs(services)

        row = curation_of(services, REMEDIO)
        print("decisão:", row.decision, "|", row.reason)
        assert row.curator_model == "gemma-3-12b-it"
        assert row.decision in ("published", "needs_review")  # o Mac respondeu e foi avaliado
        assert row.decision != "pending_curator"
    finally:
        services.close()
