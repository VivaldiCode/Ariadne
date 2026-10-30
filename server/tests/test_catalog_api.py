"""Fase 12: `/api/catalog`, a descarga pelo worker `catalog` e a remoção de fontes."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.app import create_app
from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.config import Settings
from ariadne.db.models import Job, Source
from ariadne.workers.runner import Runner
from tests.catalogfakes import FakeKiwixCatalog, make_book, serve
from tests.fakes import default_fake

pytestmark = pytest.mark.db

PT = "11111111-1111-4111-8111-111111111111"
EN = "22222222-2222-4222-8222-222222222222"
PT_NEW = "44444444-4444-4444-8444-444444444444"


class Keys:
    admin: str
    member: str


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeKiwixCatalog]:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    catalog = FakeKiwixCatalog()
    catalog.add(
        make_book(scratch, PT, "wikipedia_pt_catalogo", language="por", title="Wikipédia PT")
    )
    catalog.add(make_book(scratch, EN, "devdocs_en_python", language="eng", category="devdocs"))
    with serve(catalog):
        yield catalog


@pytest.fixture
def zims(tmp_path: Path) -> Path:
    directory = tmp_path / "zim"
    directory.mkdir()
    return directory


@pytest.fixture
def api(
    database_url: str, db: sessionmaker[Session], zims: Path, fake: FakeKiwixCatalog
) -> Iterator[TestClient]:
    with db() as session, session.begin():
        _, admin = create_principal(session, PrincipalKind.user, "admin", Role.admin)
        _, member = create_principal(session, PrincipalKind.user, "membro")
    settings = Settings(
        database_url=database_url,
        zim_dir=zims,
        kiwix_url="http://k.invalido",
        kiwix_catalog_url=fake.entries_url,
        zim_wait_served_seconds=0,
        zim_download_free_margin_mb=0,
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {admin}"}) as client:
        client.app.state.services.kiwix = default_fake().client()  # type: ignore[attr-defined]
        keys = Keys()
        keys.admin, keys.member = admin, member
        client.keys = keys  # type: ignore[attr-defined]
        yield client


def member(api: TestClient) -> dict[str, str]:
    keys: Keys = api.keys  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {keys.member}"}


def run_worker(api: TestClient) -> None:
    services = api.app.state.services  # type: ignore[attr-defined]
    runner = Runner(services, ["catalog"], stop=threading.Event())
    while runner.run_once():
        pass


def test_navegar_o_catalogo_qualquer_principal(api: TestClient) -> None:
    body = api.get("/api/catalog", headers=member(api)).json()
    assert body["total"] == 2 and {i["name"] for i in body["items"]} == {
        "wikipedia_pt_catalogo",
        "devdocs_en_python",
    }
    assert api.get("/api/catalog", params={"lang": "por"}).json()["total"] == 1
    assert api.get("/api/catalog", params={"q": "python"}).json()["items"][0]["id"] == EN
    filters = api.get("/api/catalog/filters").json()
    assert {o["code"] for o in filters["languages"]} == {"por", "eng"}
    assert api.get("/api/catalog", params={"count": 500}).status_code == 422


def test_catalogo_em_baixo_e_um_502(api: TestClient) -> None:
    api.app.state.services.get_catalog().entries_url = "http://localhost:9/catalog/v2/entries"  # type: ignore[attr-defined]
    api.app.state.services.catalog.client.timeout = 1  # type: ignore[attr-defined]
    response = api.get("/api/catalog")
    assert response.status_code == 502 and "unreachable" in response.json()["detail"]


def test_so_administradores_descarregam(api: TestClient) -> None:
    assert api.post(f"/api/catalog/{PT}/download", headers=member(api)).status_code == 403
    assert api.delete("/api/sources/zim:x", headers=member(api)).status_code == 403
    assert api.delete("/api/catalog/downloads/1", headers=member(api)).status_code == 403
    assert api.post("/api/catalog/99999999-9999-4999-8999-999999999999/download").status_code == 404
    assert api.post("/api/catalog/nao-e-um-uuid/download").status_code == 404


def test_descarregar_regista_a_fonte_e_mostra_o_progresso(
    api: TestClient, zims: Path, fake: FakeKiwixCatalog
) -> None:
    queued = api.post(f"/api/catalog/{PT}/download")
    assert queued.status_code == 202
    job = queued.json()
    assert (
        job["status"] == "pending" and job["phase"] == "queued" and job["requested_by"] == "admin"
    )
    # um segundo pedido não duplica o job pendente
    assert api.post(f"/api/catalog/{PT}/download").json()["job_id"] == job["job_id"]
    listed = api.get("/api/catalog", params={"lang": "por"}).json()["items"][0]
    assert listed["download"]["job_id"] == job["job_id"] and not listed["installed"]

    run_worker(api)

    done = api.get("/api/catalog/downloads").json()[0]
    assert done["status"] == "done" and done["phase"] == "done" and done["served"] is False
    assert done["bytes"] == len(fake.books[PT].data) and done["total"] == done["bytes"]
    assert done["source_id"] == "zim:wikipedia_pt_catalogo_maxi"
    assert (zims / "wikipedia_pt_catalogo_maxi_2026-09.zim").is_file()
    assert not list(zims.glob("*.part"))
    # ficou registada como fonte e o catálogo já a marca como instalada
    sources = {s["id"]: s for s in api.get("/api/sources").json()}
    assert sources["zim:wikipedia_pt_catalogo_maxi"]["size_bytes"] == len(fake.books[PT].data)
    entry = api.get("/api/catalog", params={"lang": "por"}).json()["items"][0]
    assert (
        entry["installed"] is True and entry["installed_source"] == "zim:wikipedia_pt_catalogo_maxi"
    )
    assert api.post(f"/api/catalog/{PT}/download").status_code == 409  # já instalado


def test_versao_mais_nova_aparece_como_atualizacao(
    api: TestClient, zims: Path, fake: FakeKiwixCatalog, tmp_path: Path
) -> None:
    api.post(f"/api/catalog/{PT}/download")
    run_worker(api)
    scratch = tmp_path / "scratch2"
    scratch.mkdir()
    fake.add(make_book(scratch, PT_NEW, "wikipedia_pt_catalogo", date="2026-10", language="por"))
    items = {i["id"]: i for i in api.get("/api/catalog", params={"lang": "por"}).json()["items"]}
    assert items[PT]["installed"] and not items[PT]["update_available"]
    assert items[PT_NEW]["update_available"] and items[PT_NEW]["installed_version"] == "2026-09"


def test_descarga_que_falha_fica_registada_com_o_motivo(
    api: TestClient, zims: Path, fake: FakeKiwixCatalog
) -> None:
    fake.books[PT].data += b"lixo"  # o sha-256 anunciado (calculado com os dados) já não bate...
    fake.books[PT].advertise_sha = True
    # ... mas o tamanho e o hash do meta4 acompanham `data`: forçamos o desencontro de outra forma
    meta4 = fake.meta4

    def wrong(book):  # type: ignore[no-untyped-def]
        return meta4(book).replace(book.sha256, "0" * 64)

    fake.meta4 = wrong  # type: ignore[method-assign]
    api.post(f"/api/catalog/{PT}/download")
    runner = Runner(api.app.state.services, ["catalog"], stop=threading.Event())  # type: ignore[attr-defined]
    assert runner.run_once()
    job = api.get("/api/catalog/downloads").json()[0]
    assert job["status"] == "pending" and "sha-256" in job["error"]  # volta a tentar com recuo
    assert not list(zims.iterdir())  # nada a meio: nem .zim nem .part


def test_cancelar_uma_descarga_pendente(api: TestClient) -> None:
    job = api.post(f"/api/catalog/{PT}/download").json()
    assert api.delete(f"/api/catalog/downloads/{job['job_id']}").json() == {"result": "cancelled"}
    assert api.get("/api/catalog/downloads").json()[0]["status"] == "failed"
    assert api.delete("/api/catalog/downloads/999999").status_code == 404


def test_arquivar_apagar_e_restaurar(
    api: TestClient, zims: Path, db: sessionmaker[Session]
) -> None:
    api.post(f"/api/catalog/{PT}/download")
    run_worker(api)
    source_id = "zim:wikipedia_pt_catalogo_maxi"
    file = zims / "wikipedia_pt_catalogo_maxi_2026-09.zim"
    assert file.is_file()

    # apagar o ficheiro sem confirmação é recusado e não toca em nada
    refused = api.delete(f"/api/sources/{source_id}", params={"delete_file": True})
    assert refused.status_code == 409 and "confirma" in refused.json()["detail"]
    wrong = api.delete(
        f"/api/sources/{source_id}", params={"delete_file": True, "confirm": "outro"}
    )
    assert wrong.status_code == 409 and file.is_file()

    archived = api.delete(f"/api/sources/{source_id}").json()
    assert archived["action"] == "archived"
    assert not file.exists() and (zims / ".archive" / file.name).is_file()
    with db() as session:
        assert session.get(Source, source_id).status == "archived"  # type: ignore[union-attr]
    assert source_id not in {s["id"] for s in api.get("/api/sources").json()}
    # uma nova sincronização não a "ressuscita" nem a marca como em falta
    api.app.state.services.registry.sync(api.app.state.services.sessions())  # type: ignore[attr-defined]
    with db() as session:
        assert session.get(Source, source_id).status == "archived"  # type: ignore[union-attr]

    restored = api.post(f"/api/sources/{source_id}/restore").json()
    assert restored["action"] == "restored" and file.is_file()
    assert source_id in {s["id"] for s in api.get("/api/sources").json()}

    deleted = api.delete(
        f"/api/sources/{source_id}", params={"delete_file": True, "confirm": source_id}
    ).json()
    assert deleted["action"] == "deleted"
    assert not file.exists() and not list((zims / ".archive").glob("*.zim"))


def test_so_remove_zim_da_pasta(api: TestClient, db: sessionmaker[Session]) -> None:
    assert api.delete("/api/sources/zim:nao-existe").status_code == 404
    with db() as session, session.begin():
        session.add(
            Source(
                id="course:x", kind="course", title="x", version="1", wing="x", license_class="open"
            )
        )
    response = api.delete("/api/sources/course:x")
    assert response.status_code == 409 and "ZIM" in response.json()["detail"]
    with db() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "catalog_download")) is None


def test_cancelar_uma_descarga_em_curso_deixa_o_part_e_o_worker_a_parar_devolve_o_job(
    api: TestClient, zims: Path
) -> None:
    from ariadne import queue
    from ariadne.queue import Defer, Permanent
    from ariadne.workers.catalog import download_job

    services = api.app.state.services  # type: ignore[attr-defined]
    api.post(f"/api/catalog/{PT}/download")
    with services.sessions() as session:
        claimed = queue.claim(session, ["catalog_download"], "teste")
        session.commit()
    assert claimed is not None
    # o pedido de cancelamento chega com o job a correr
    with services.sessions() as session:
        job = session.get(Job, claimed.id)
        assert job is not None and queue_cancel(session, job) == "cancelling"
        session.commit()
    with pytest.raises(Permanent, match="cancelled"):
        download_job(services, claimed, threading.Event())
    assert not list(zims.glob("*.part")) and not list(zims.glob("*.zim"))  # cancelada: sem lixo
    assert api.get("/api/catalog/downloads").json()[0]["phase"] == "cancelled"

    # o worker a parar (SIGTERM): devolve o job à fila, sem o dar por falhado
    for part in zims.glob("*.part"):  # (o .part inteiro, de um ficheiro tão pequeno, até concluía)
        part.unlink()
    stopping = threading.Event()
    stopping.set()
    with pytest.raises(Defer):
        download_job(services, claimed, stopping)


def queue_cancel(session: Session, job: Job) -> str:
    from ariadne.workers.catalog import request_cancel

    return request_cancel(session, job)


def test_sem_sha256_a_descarga_falha_de_vez_e_so_um_administrador_a_permite(
    api: TestClient, zims: Path, fake: FakeKiwixCatalog
) -> None:
    fake.books[PT].advertise_sha = False
    queued = api.post(f"/api/catalog/{PT}/download").json()
    run_worker(api)
    job = api.get("/api/catalog/downloads").json()[0]
    assert job["status"] == "failed" and "sha-256" in job["error"]  # definitivo: não repete
    assert not list(zims.iterdir())
    again = api.post(f"/api/catalog/{PT}/download", params={"allow_unverified": True}).json()
    assert again["job_id"] != queued["job_id"]
    run_worker(api)
    assert (zims / "wikipedia_pt_catalogo_maxi_2026-09.zim").is_file()
    assert (
        api.post(
            f"/api/catalog/{EN}/download", params={"allow_unverified": True}, headers=member(api)
        ).status_code
        == 403
    )


def test_o_mesmo_livro_nao_se_descarrega_duas_vezes_nem_com_o_job_a_correr(
    api: TestClient, db: sessionmaker[Session]
) -> None:
    from ariadne import queue

    first = api.post(f"/api/catalog/{PT}/download").json()["job_id"]
    with db() as session:
        claimed = queue.claim(session, ["catalog_download"], "w1")
        session.commit()
    assert claimed is not None and claimed.id == first  # agora está «a correr»
    second = api.post(f"/api/catalog/{PT}/download").json()["job_id"]
    assert second == first
    with db() as session:
        assert session.scalars(select(Job.id).where(Job.kind == "catalog_download")).all() == [
            first
        ]


def test_um_agente_nao_ve_o_erro_nem_quem_pediu(
    api: TestClient, db: sessionmaker[Session], fake: FakeKiwixCatalog
) -> None:
    fake.books[PT].advertise_sha = False
    api.post(f"/api/catalog/{PT}/download")
    run_worker(api)
    with db() as session, session.begin():
        _, agent = create_principal(session, PrincipalKind.agent, "agente")
    headers = {"Authorization": f"Bearer {agent}"}
    seen = api.get("/api/catalog/downloads", headers=headers).json()[0]
    assert seen["status"] == "failed" and seen["error"] is None and seen["requested_by"] is None
    item = api.get("/api/catalog", params={"lang": "por"}, headers=headers).json()["items"][0]
    assert item["download"]["error"] is None and item["download"]["requested_by"] is None
    mine = api.get("/api/catalog/downloads").json()[0]
    assert mine["requested_by"] == "admin" and mine["error"]


def test_apaga_os_part_abandonados_mas_nao_os_de_descargas_ativas(
    api: TestClient, zims: Path
) -> None:
    import os
    import time

    from ariadne.workers.catalog import sweep_stale_parts

    old = zims / "velho.zim.part"
    old.write_bytes(b"x")
    recent = zims / "recente.zim.part"
    recent.write_bytes(b"x")
    long_ago = time.time() - 5 * 86400
    os.utime(old, (long_ago, long_ago))
    assert sweep_stale_parts(api.app.state.services) == 1  # type: ignore[attr-defined]
    assert not old.exists() and recent.exists()
