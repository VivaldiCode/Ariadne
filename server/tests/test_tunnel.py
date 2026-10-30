"""O ciclo de vida do túnel do lote: o cloudflared só corre enquanto uma corrida tem token."""

# ruff: noqa: F811  (os fixtures importados são redefinidos pelos parâmetros dos testes)
import json
import re
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ariadne.compute import control, tick
from ariadne.compute.gateway import create_gateway_app
from ariadne.compute.tunnel import (
    INGRESS_PATH,
    TunnelSupervisor,
    cloudflared_starter,
    tunnel_config,
    tunnel_needed,
)
from ariadne.db.models import BatchRun
from ariadne.services import Services
from tests.gpufakes import FakeGpuBackend
from tests.test_compute import (  # noqa: F401  (fixtures)
    curator,
    lab,
    pending_curation_jobs,
    zims,
)
from tests.test_gateway import auth, launch
from tests.wikifakes import FakeWiki

SECRET = "segredo-de-teste"


class FakeProcess:
    def __init__(self) -> None:
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 0

    def kill(self) -> None:
        self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.code if self.code is not None else 0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def supervisor(
    answers: list[bool | Exception],
) -> tuple[TunnelSupervisor, list[FakeProcess], Clock]:
    started: list[FakeProcess] = []
    clock = Clock()

    def needed() -> bool:
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def start() -> FakeProcess:
        started.append(FakeProcess())
        return started[-1]

    return TunnelSupervisor(needed, start, linger=45, clock=clock), started, clock


def credentials(tmp_path: Path) -> Path:
    path = tmp_path / "tunnel.json"
    path.write_text(
        json.dumps({"AccountTag": "conta", "TunnelID": "1234-abcd", "TunnelSecret": SECRET}),
        encoding="utf-8",
    )
    return path


# --- o supervisor (sem base nem cloudflared) ----------


def test_sem_corridas_o_cloudflared_nunca_arranca() -> None:
    sup, started, clock = supervisor([False])
    for _ in range(5):
        sup.step()
        clock.now += 10
    assert started == []


def test_liga_com_uma_corrida_e_desliga_depois_do_linger() -> None:
    sup, started, clock = supervisor([True, True, False])
    sup.step()
    assert len(started) == 1 and sup.running
    clock.now += 10
    sup.step()
    assert len(started) == 1  # não arranca outro
    clock.now += 10
    sup.step()  # já não precisa, mas o `finish` ainda tem de voltar pelo túnel
    assert sup.running
    clock.now += 40
    sup.step()
    assert not sup.running and started[0].terminated


def test_se_o_cloudflared_morre_a_meio_da_corrida_volta_a_subir() -> None:
    sup, started, clock = supervisor([True])
    sup.step()
    started[0].code = 1
    clock.now += 10
    sup.step()
    assert len(started) == 2 and sup.running


def test_sem_base_fecha_por_omissao() -> None:
    sup, started, clock = supervisor([True, RuntimeError("base em baixo")])
    sup.step()
    clock.now += 50
    sup.step()
    assert not sup.running and started[0].terminated


def test_run_desliga_o_cloudflared_ao_parar() -> None:
    sup, started, _clock = supervisor([True])
    stop = threading.Event()
    thread = threading.Thread(target=sup.run, args=(stop, 0.01))
    thread.start()
    while not started:
        stop.wait(0.01)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and started[0].terminated


def test_um_cloudflared_teimoso_e_morto() -> None:
    class Stubborn(FakeProcess):
        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            if self.code is None:
                raise subprocess.TimeoutExpired("cloudflared", timeout or 0)
            return self.code

    process = Stubborn()
    sup = TunnelSupervisor(lambda: True, lambda: process)
    sup.step()
    sup.stop()
    assert process.code == -9


# --- a configuração do cloudflared ----------


def test_o_ingress_so_deixa_passar_as_quatro_rotas(tmp_path: Path) -> None:
    config = tunnel_config("https://gpu-lote.exemplo.pt", str(credentials(tmp_path)), port=8810)
    assert config["tunnel"] == "1234-abcd"
    assert config["metrics"].startswith("localhost:")
    first, last = config["ingress"]
    assert first["hostname"] == "gpu-lote.exemplo.pt"
    assert first["service"] == "http://localhost:8810"  # o gateway, no próprio contentor
    assert last == {"service": "http_status:404"}
    allowed = [f"/gpu/v1/runs/7/{route}" for route in ("tasks", "results", "heartbeat", "finish")]
    denied = [
        "/",
        "/api/compute",
        "/gpu/v1/runs/7",
        "/gpu/v1/runs/x/tasks",
        "/gpu/v1/runs/7/tasks/../../api",
        "/docs",
        "/gpu/v1/runs/7/finishes",
    ]
    assert all(re.match(first["path"], path) for path in allowed)
    assert not any(re.match(first["path"], path) for path in denied)
    assert first["path"] == INGRESS_PATH


def test_a_configuracao_gravada_nao_leva_o_segredo(tmp_path: Path) -> None:
    creds = credentials(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    cloudflared_starter(tunnel_config("https://gpu.exemplo.pt", str(creds), port=8810), work)
    written = (work / "cloudflared.yml").read_text(encoding="utf-8")
    assert SECRET not in written and str(creds) in written
    assert json.loads(written)["credentials-file"] == str(creds)


def test_credenciais_sem_tunnel_id_ou_url_sem_host_recusam(tmp_path: Path) -> None:
    bad = tmp_path / "token.json"
    bad.write_text(json.dumps({"a": "b"}), encoding="utf-8")
    with pytest.raises(ValueError, match="TunnelID"):
        tunnel_config("https://gpu.exemplo.pt", str(bad), port=8810)
    with pytest.raises(ValueError, match="hostname"):
        tunnel_config("gpu.exemplo.pt", str(credentials(tmp_path)), port=8810)


# --- com a base: a corrida manda no túnel ----------


@pytest.mark.db
@pytest.mark.usefixtures("short_articles")
def test_a_corrida_liga_e_o_finish_desliga_o_tunel(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab

    def needed() -> bool:
        with services.sessions() as session:
            return tunnel_needed(session)

    started: list[FakeProcess] = []
    clock = Clock()

    def start() -> FakeProcess:
        started.append(FakeProcess())
        return started[-1]

    sup = TunnelSupervisor(needed, start, linger=45, clock=clock)
    sup.step()
    assert started == []  # sem corridas: nada exposto

    run_id, token = launch(services, gpu)
    sup.step()
    assert sup.running

    with TestClient(create_gateway_app(services.sessions)) as gw:
        finished = gw.post(f"/gpu/v1/runs/{run_id}/finish", json={}, headers=auth(token))
    assert finished.status_code == 200
    clock.now += 10
    sup.step()
    assert sup.running  # ainda dentro do linger
    clock.now += 45
    sup.step()
    assert not sup.running


@pytest.mark.db
@pytest.mark.usefixtures("short_articles")
def test_token_expirado_ou_corrida_cancelada_nao_pedem_tunel(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    run_id, _token = launch(services, gpu)
    with services.sessions() as session:
        assert tunnel_needed(session)
        run = session.get(BatchRun, run_id)
        assert run is not None
        assert not tunnel_needed(session, now=run.token_expires_at + timedelta(seconds=1))  # type: ignore[operator]
        assert tunnel_needed(session, now=datetime.now(UTC))
    with services.sessions() as session:
        control.set_state(session, "stopped", "teste")
        session.commit()
    tick.tick(services)  # cancelar revoga o token
    with services.sessions() as session:
        assert not tunnel_needed(session)


def test_se_o_cloudflared_nao_arranca_tenta_na_volta_seguinte() -> None:
    attempts: list[int] = []

    def start() -> FakeProcess:
        attempts.append(1)
        if len(attempts) == 1:
            raise FileNotFoundError("cloudflared")
        return FakeProcess()

    sup = TunnelSupervisor(lambda: True, start)
    sup.step()
    assert not sup.running
    sup.step()
    assert sup.running and len(attempts) == 2
