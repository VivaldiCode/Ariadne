"""A API do painel de controlo: estados, destinos, lote manual (Colab) e revisão em lote."""

import io
import json
import zipfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from ariadne.auth import PrincipalKind, Role, create_principal
from ariadne.compute import tick
from tests.curationzims import MALICIOSO
from tests.gpufakes import FakeGpuBackend
from tests.test_curation import request_import, run_jobs
from tests.test_curation_api import (  # noqa: F401  (fixtures)
    api,
    curator,
    services_of,
    wiki_of,
    zims,
)
from tests.wikifakes import FakeCurator

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]

SEGREDO = "token-que-nunca-deve-aparecer"


@pytest.fixture
def panel(api: TestClient, db: sessionmaker[Session]) -> Iterator[TestClient]:  # noqa: F811
    """O cliente do painel é admin (as escritas da computação são só de admin); uma chave `user`
    não-admin fica em `member_key`."""
    services = services_of(api)
    remote = FakeCurator()
    services.backends = {**services.get_backends(), "kaggle": FakeGpuBackend(remote)}
    with db() as session, session.begin():
        _, member_key = create_principal(session, PrincipalKind.user, "membro", Role.member)
    api.member_key = member_key  # type: ignore[attr-defined]
    yield api
    remote.stop()


def seed(panel: TestClient) -> None:
    services = services_of(panel)
    request_import(services, MALICIOSO)
    run_jobs(services, ("importer",))


def test_so_principais_user_usam_o_painel(panel: TestClient) -> None:
    agent = {"Authorization": f"Bearer {panel.agent_key}"}  # type: ignore[attr-defined]
    assert panel.get("/api/compute", headers=agent).status_code == 403
    assert (
        panel.post("/api/compute/state", headers=agent, json={"action": "pause"}).status_code == 403
    )
    assert panel.get("/api/compute").status_code == 200


def test_so_admin_lanca_lotes_abre_o_tunel_e_gasta_gpu(panel: TestClient) -> None:
    """Um `user` não-admin vê o painel, mas não muda o destino, não dá play, não mexe na quota,
    não gera lotes nem pacotes (o pacote com túnel cria um token novo) nem carrega ou cancela."""
    member = {"Authorization": f"Bearer {panel.member_key}"}  # type: ignore[attr-defined]
    assert panel.get("/api/compute", headers=member).status_code == 200
    assert panel.get("/api/compute/runs", headers=member).status_code == 200
    before = panel.get("/api/compute").json()
    escritas = [
        ("PUT", "/api/compute/control", {"destination": "kaggle"}),
        ("POST", "/api/compute/state", {"action": "play", "ignore_window": True}),
        ("PUT", "/api/compute/backends/kaggle/config", {"weekly_hours": 30}),
        ("POST", "/api/compute/runs", {"backend": "colab"}),
        ("GET", "/api/compute/runs/1/package", None),
        ("POST", "/api/compute/runs/1/results", None),
        ("POST", "/api/compute/runs/1/cancel", None),
    ]
    for method, path, body in escritas:
        response = panel.request(method, path, headers=member, json=body)
        assert response.status_code == 403, (method, path, response.status_code)
    after = panel.get("/api/compute").json()
    assert (after["control"], after["backends"]) == (before["control"], before["backends"])


def test_o_destino_local_e_o_mac_e_diz_se_esta_offline(
    panel: TestClient,
    curator: FakeCurator,  # noqa: F811
) -> None:
    def local() -> dict[str, object]:
        backends = {b["id"]: b for b in panel.get("/api/compute").json()["backends"]}
        return backends["local"]  # type: ignore[no-any-return]

    assert local()["label"] == "Mac (Metal)" and local()["online"] is True
    curator.health_ok = False  # o Mac desligou-se (ou ainda a carregar o modelo)
    services_of(panel).curator_status(force=True)
    offline = local()
    assert offline["online"] is False and "loading" in str(offline["status"])
    # Os destinos externos não têm «online»: é uma propriedade do Mac.
    others = [b for b in panel.get("/api/compute").json()["backends"] if b["id"] != "local"]
    assert all(b["online"] is None for b in others)


def test_visao_geral_mostra_destinos_e_nunca_credenciais(panel: TestClient) -> None:
    services_of(panel).settings.kaggle_api_token = SEGREDO
    services_of(panel).settings.kaggle_username = "dono"
    response = panel.get("/api/compute")
    assert SEGREDO not in response.text
    body = response.json()
    assert body["control"]["state"] == "running" and body["control"]["destination"] == "local"
    assert body["control"]["local_can_work"] is True
    backends = {b["id"]: b for b in body["backends"]}
    assert set(backends) == {"local", "kaggle", "colab", "paid"}
    assert backends["local"]["configured"] is True  # CURATOR_URL do teste
    assert backends["paid"]["implemented"] is False
    assert backends["colab"]["mode"] == "manual"
    assert [k["kind"] for k in body["kinds"]] == ["curate_page", "domain_classify"]
    assert body["run"] is None and body["review"] == {"needs_review": 0, "from_batch": 0}


def test_play_pausa_retoma_e_para(panel: TestClient) -> None:
    def state(action: str, **extra: object) -> dict[str, object]:
        response = panel.post("/api/compute/state", json={"action": action, **extra})
        assert response.status_code == 200
        return response.json()  # type: ignore[no-any-return]

    paused = state("pause")
    assert paused["state"] == "paused" and paused["active_now"] is False
    assert paused["local_can_work"] is False
    resumed = state("resume")
    assert resumed["state"] == "running" and resumed["local_can_work"] is True
    stopped = state("stop")
    assert stopped["state"] == "stopped" and stopped["local_can_work"] is False
    assert state("play", ignore_window=True)["manual_override"] is True
    assert state("pause")["manual_override"] is False  # a pausa limpa o arranque manual


def test_destino_e_janela(panel: TestClient) -> None:
    response = panel.put(
        "/api/compute/control",
        json={
            "destination": "kaggle",
            "schedule_enabled": True,
            "window_start": "23:00",
            "window_end": "07:00",
            "batch_size": 50,
        },
    )
    body = response.json()
    assert response.status_code == 200 and body["destination"] == "kaggle"
    assert body["schedule_enabled"] and body["next_window_start"] is not None
    assert panel.put("/api/compute/control", json={"destination": "paid"}).status_code == 400
    assert panel.put("/api/compute/control", json={"destination": "nada"}).status_code == 400
    assert panel.put("/api/compute/control", json={"window_start": "25:61"}).status_code == 400
    assert panel.put("/api/compute/control", json={"timezone": "Marte"}).status_code == 400
    assert panel.put("/api/compute/control", json={"batch_size": 0}).status_code == 400
    assert panel.put("/api/compute/control", json={"disabled_kinds": ["x"]}).status_code == 400


def test_configuracao_do_destino_e_horas_ja_gastas(panel: TestClient) -> None:
    response = panel.put(
        "/api/compute/backends/kaggle/config", json={"weekly_hours": 30, "used_hours": 12.5}
    )
    assert response.status_code == 200
    quota = response.json()["quota"]
    assert quota["weekly_hours"] == 30 and quota["used_hours"] == pytest.approx(12.5, abs=0.01)
    assert quota["remaining_hours"] == pytest.approx(17.5, abs=0.01)
    assert panel.put("/api/compute/backends/paid/config", json={}).status_code == 404
    assert (
        panel.put("/api/compute/backends/kaggle/config", json={"weekly_hours": -1}).status_code
        == 422
    )


def test_o_painel_recusa_um_modelo_chines(panel: TestClient) -> None:
    refused = panel.put(
        "/api/compute/backends/kaggle/config", json={"model": "Qwen/Qwen2.5-7B-Instruct-AWQ"}
    )
    assert refused.status_code == 422 and "excluded from Ariadne" in refused.text
    accepted = panel.put(
        "/api/compute/backends/kaggle/config", json={"model": "mistralai/Mistral-7B-Instruct-v0.3"}
    )
    assert accepted.status_code == 200


def test_lote_manual_do_colab_ate_a_revisao_em_lote(panel: TestClient) -> None:
    seed(panel)
    created = panel.post("/api/compute/runs", json={"backend": "colab"})
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == "awaiting_results" and run["manual"] and run["task_count"] == 1
    assert panel.post("/api/compute/runs", json={"backend": "colab"}).status_code == 409
    assert panel.post("/api/compute/runs", json={"backend": "kaggle"}).status_code == 400

    package = panel.get(f"/api/compute/runs/{run['id']}/package")
    assert package.status_code == 200 and package.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(package.content))
    lines = archive.read("tasks.jsonl").decode().splitlines()
    assert json.loads(archive.read("manifest.json"))["run_id"] == run["id"]

    remote = FakeCurator()
    try:
        results = "\n".join(
            json.dumps(
                {
                    "id": json.loads(t)["id"],
                    "content": remote.reply({"messages": json.loads(t)["messages"]}),
                }
            )
            for t in lines
        )
    finally:
        remote.stop()
    garbage = panel.post(f"/api/compute/runs/{run['id']}/results", content=b"lixo\n")
    assert garbage.status_code == 409
    # O notebook do Colab devolve um zip com o resultado e o meta.
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("results.jsonl", results)
        z.writestr("results.meta.json", json.dumps({"run_id": run["id"], "gpu_seconds": 900}))
    uploaded = panel.post(f"/api/compute/runs/{run['id']}/results", content=out.getvalue())
    assert uploaded.status_code == 200 and uploaded.json()["status"] == "importing"

    tick.tick(services_of(panel))  # o worker importa
    done = panel.get(f"/api/compute/runs/{run['id']}").json()
    assert done["status"] == "done" and done["review_count"] == 1 and done["gpu_seconds"] == 900

    overview = panel.get("/api/compute").json()
    assert overview["review"] == {"needs_review": 1, "from_batch": 1}
    listed = panel.get("/api/curation", params={"batch_run_id": run["id"]}).json()["items"]
    assert [i["document_id"] for i in listed] == [MALICIOSO] and listed[0]["batch_run_id"] == run[
        "id"
    ]
    assert panel.get("/api/curation", params={"from_batch": True}).json()["items"]

    item = listed[0]
    stale = {"document_id": item["document_id"], "fingerprint": "outra"}
    again = panel.post("/api/curation/review-batch", json={"items": [stale], "decision": "approve"})
    assert again.json() == {"done": [], "conflicts": [MALICIOSO], "missing": []}
    ok = {"document_id": item["document_id"], "fingerprint": item["fingerprint"]}
    approved = panel.post(
        "/api/curation/review-batch",
        json={
            "items": [ok, {"document_id": "nao/existe", "fingerprint": "x"}],
            "decision": "approve",
        },
    )
    assert approved.json() == {"done": [MALICIOSO], "conflicts": [], "missing": ["nao/existe"]}
    run_jobs(services_of(panel), ("importer",))
    assert "Resumo de referência." in next(iter(wiki_of(panel).pages.values())).content


def test_cancelar_uma_corrida(panel: TestClient) -> None:
    seed(panel)
    run = panel.post("/api/compute/runs", json={"backend": "colab"}).json()
    assert panel.post("/api/compute/runs/999/cancel").status_code == 404
    cancelled = panel.post(f"/api/compute/runs/{run['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    assert panel.post(f"/api/compute/runs/{run['id']}/cancel").status_code == 409
    assert panel.post("/api/compute/runs", json={"backend": "colab"}).status_code == 201  # liberta


def test_zip_enorme_e_recusado_no_upload(
    panel: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ariadne.compute import runs as runs_module

    seed(panel)
    run = panel.post("/api/compute/runs", json={"backend": "colab"}).json()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("results.jsonl", b"0" * 5000)
    monkeypatch.setattr(runs_module, "MAX_RESULTS_UNZIPPED", 1000)
    refused = panel.post(f"/api/compute/runs/{run['id']}/results", content=out.getvalue())
    assert refused.status_code == 400 and "uncompressed" in refused.json()["detail"]
