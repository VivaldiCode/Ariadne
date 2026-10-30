"""O Mac nem sempre está ligado: o curador é consultado em cache, o worker não reclama `curate_page`
enquanto ele não responde (os jobs ficam intactos) e o painel mostra «Mac offline»."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.curation import monitor
from ariadne.curation.monitor import CuratorMonitor
from ariadne.db.models import Job
from ariadne.services import Services
from tests.curationzims import MALICIOSO, make_dir
from tests.test_curation import build, curation_of, request_import, run_jobs
from tests.wikifakes import FakeCurator, FakeWiki

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]


@pytest.fixture
def curator() -> Iterator[FakeCurator]:
    fake = FakeCurator()
    yield fake
    fake.stop()


@pytest.fixture
def lab(
    db: sessionmaker[Session], tmp_path: Path, curator: FakeCurator
) -> Iterator[tuple[Services, FakeWiki]]:
    services, wiki = build(
        db, make_dir(tmp_path / "zims"), curator_url=curator.url, curator_model="gemma-falso"
    )
    yield services, wiki
    services.close()


def curate_jobs(services: Services) -> list[Job]:
    with services.sessions() as session:
        return list(session.scalars(select(Job).where(Job.kind == "curate_page")))


def test_probe_distingue_online_a_carregar_e_sem_resposta(curator: FakeCurator) -> None:
    assert monitor.probe(curator.url) == (True, "responding")
    curator.health_ok = False
    online, detail = monitor.probe(curator.url)
    assert online is False and "loading" in detail
    curator.stop()
    online, detail = monitor.probe(curator.url)
    assert online is False and "no response" in detail

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.url.path == "/health" else 404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert monitor.probe("http://mac:8080", client)[0] is True


def test_o_monitor_guarda_em_cache_e_mark_down_vale_ja(curator: FakeCurator) -> None:
    watch = CuratorMonitor()
    assert watch.status("").online is None and watch.status("").configured is False
    assert watch.status(curator.url).online is True
    curator.health_ok = False
    assert watch.status(curator.url).online is True  # ainda dentro da cache de um minuto
    assert watch.status(curator.url, force=True).online is False
    curator.health_ok = True
    watch.mark_down(curator.url, "falhou um pedido")
    status = watch.status(curator.url)
    assert status.online is False and status.detail == "falhou um pedido"


def test_mac_offline_os_jobs_ficam_na_fila_sem_gastar_tentativas(
    lab: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, wiki = lab
    curator.health_ok = False  # o Mac está desligado (ou o modelo ainda a carregar)

    request_import(services, MALICIOSO)
    run_jobs(services)

    assert curation_of(services, MALICIOSO).decision == "pending_curator"
    [job] = curate_jobs(services)
    assert job.status == "pending" and job.attempts == 0 and job.last_error is None
    assert curator.requests == [] and wiki.pages == {}

    # O Mac volta: a fila arranca sozinha e o item é tratado.
    curator.health_ok = True
    services.curator_status(force=True)
    run_jobs(services)
    assert curation_of(services, MALICIOSO).decision != "pending_curator"
    assert curator.requests


def test_o_mac_cai_a_meio_e_o_job_adia_sem_perder_tentativas(
    lab: tuple[Services, FakeWiki], curator: FakeCurator
) -> None:
    services, _wiki = lab
    request_import(services, MALICIOSO)
    curator.mode = "down"  # o /health ainda responde, o pedido já não

    run_jobs(services)

    [job] = curate_jobs(services)
    assert job.status == "pending" and job.attempts == 0 and "503" in (job.last_error or "")
    status = services.curator_status()
    assert status.online is False and "503" in status.detail  # o seguinte nem tenta
    assert curation_of(services, MALICIOSO).decision == "pending_curator"
