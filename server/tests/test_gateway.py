"""O gateway do lote (túnel): autenticação por corrida, limites, e o caminho completo."""

# ruff: noqa: F811  (os fixtures importados são redefinidos pelos parâmetros dos testes)
import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from ariadne import queue
from ariadne.compute import control, gateway, runs, tick
from ariadne.compute.gateway import create_gateway_app
from ariadne.db.models import BatchRun, Curation, Job
from ariadne.db.session import make_engine
from ariadne.services import Services
from tests.curationzims import MALICIOSO
from tests.gpufakes import FakeGpuBackend
from tests.ipfake import ip
from tests.test_compute import (  # noqa: F401  (fixtures)
    curator,
    go_external,
    lab,
    pending_curation_jobs,
    seed,
    zims,
)
from tests.test_curation import curation_of
from tests.wikifakes import FakeWiki

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]

PUBLIC_URL = "https://gpu.exemplo.pt"


def launch(services: Services, gpu: FakeGpuBackend) -> tuple[int, str]:
    """Semeia, lança a corrida e devolve (id, token) como o notebook os leria no manifesto."""
    services.settings.gpu_gateway_public_url = PUBLIC_URL
    services.settings.gpu_gateway_access_id = "id-access"
    services.settings.gpu_gateway_access_secret = "segredo-access"
    seed(services, MALICIOSO)
    go_external(services)
    tick.tick(services)
    [spec] = gpu.submitted
    info = spec.manifest["gateway"]
    assert info["url"] == PUBLIC_URL
    assert info["headers"] == {
        "CF-Access-Client-Id": "id-access",
        "CF-Access-Client-Secret": "segredo-access",
    }
    return spec.run_id, str(info["token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def gw(lab: tuple[Services, FakeWiki, FakeGpuBackend]) -> Iterator[TestClient]:
    services, _wiki, _gpu = lab
    with TestClient(create_gateway_app(services.sessions)) as client:
        yield client


def results_of(gpu: FakeGpuBackend) -> bytes:
    return gpu.fetch({}).results_jsonl


# --- o caminho completo ----------


def test_o_manifesto_leva_o_tunel_e_o_token_nao_fica_guardado(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    assert token.startswith("gpugw_") and len(token) > 40
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        assert run is not None and run.token_hash == gateway.hash_token(token)
        assert token not in (run.token_hash or "")
        assert run.token_expires_at is not None and run.token_expires_at > datetime.now(UTC)


def test_puxar_enviar_progresso_e_fechar(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    base = f"/gpu/v1/runs/{run_id}"

    pulled = gw.get(f"{base}/tasks", headers=auth(token))
    assert pulled.status_code == 200 and pulled.headers["content-type"].startswith(
        "application/x-ndjson"
    )
    assert pulled.text.encode() == gpu.submitted[0].tasks_jsonl
    first = gw.get(f"{base}/tasks", params={"offset": 0, "limit": 1}, headers=auth(token))
    assert len(first.text.splitlines()) == 1

    beat = gw.post(
        f"{base}/heartbeat", json={"done": 1, "total": 1, "gpu_seconds": 321}, headers=auth(token)
    )
    assert beat.status_code == 200
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        assert run is not None
        assert run.status == "running" and run.progress == {
            "done": 1,
            "total": 1,
            "gpu_seconds": 321,
        }
        assert run.last_heartbeat_at is not None and run.running_at is not None

    body = results_of(gpu)
    sent = gw.post(f"{base}/results", content=body, headers=auth(token))
    assert sent.status_code == 200 and sent.json()["accepted"] == len(body.splitlines())
    again = gw.post(f"{base}/results", content=body, headers=auth(token))  # repetir não estraga
    assert again.status_code == 200

    done = gw.post(f"{base}/finish", json={"gpu_seconds": 400}, headers=auth(token))
    assert done.json() == {"status": "importing"}
    # O token morre com o fim da corrida.
    assert gw.get(f"{base}/tasks", headers=auth(token)).status_code == 401

    tick.tick(services)  # o worker importa
    with services.sessions() as session:
        run = session.scalars(select(BatchRun)).one()
        assert run.status == "done" and run.review_count == 1 and run.token_hash is None
        assert run.gpu_seconds == 400 or run.progress.get("gpu_seconds") == 400
    row = curation_of(services, MALICIOSO)
    assert (
        row.decision == "needs_review" and row.batch_run_id == run_id
    )  # como o local: revisão humana
    [job] = pending_curation_jobs(services)
    assert job.status == "done"
    assert gpu.cleaned


def test_resultados_do_tunel_e_do_ficheiro_juntam_se(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    lines = results_of(gpu).splitlines(keepends=True)
    assert (
        gw.post(f"/gpu/v1/runs/{run_id}/results", content=lines[0], headers=auth(token)).status_code
        == 200
    )
    # O túnel só levou o primeiro pedido; o resto chega pelo output do Kaggle e junta-se.
    gpu.drop = {"never"}
    for _ in range(3):
        tick.tick(services)
    with services.sessions() as session:
        run = session.scalars(select(BatchRun)).one()
        assert run.status == "done" and run.review_count == 1


def test_um_destino_que_falha_aproveita_o_que_o_tunel_ja_recebeu(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    body = results_of(gpu)
    assert (
        gw.post(f"/gpu/v1/runs/{run_id}/results", content=body, headers=auth(token)).status_code
        == 200
    )
    gpu.states = ["failed"]  # o kernel foi morto antes de acabar
    tick.tick(services)
    tick.tick(services)
    with services.sessions() as session:
        run = session.scalars(select(BatchRun)).one()
        assert run.status == "done" and run.review_count == 1  # não falhou: importou o que havia
        assert "erro" in (run.error or "") or run.error is None
    assert curation_of(services, MALICIOSO).decision == "needs_review"


def test_prazo_esgotado_tambem_aproveita_os_resultados_parciais(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    gpu.states = ["running"]
    run_id, token = launch(services, gpu)
    gw.post(f"/gpu/v1/runs/{run_id}/results", content=results_of(gpu), headers=auth(token))
    tick.tick(services, datetime.now(UTC) + timedelta(hours=11))
    with services.sessions() as session:
        run = session.scalars(select(BatchRun)).one()
        assert run.status == "done" and run.review_count == 1


# --- autenticação e limites ----------


def test_so_o_token_da_propria_corrida_serve(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    path = f"/gpu/v1/runs/{run_id}/tasks"
    assert gw.get(path).status_code == 401  # sem token
    assert gw.get(path, headers=auth("gpugw_errado")).status_code == 401
    assert gw.get(path, headers=auth(token.removeprefix("gpugw_"))).status_code == 401
    assert gw.get(path, headers={"Authorization": f"Basic {token}"}).status_code == 401
    # Outra corrida (ou uma que não existe): a mesma resposta, sem dizer se existe.
    other = gw.get(f"/gpu/v1/runs/{run_id + 1}/tasks", headers=auth(token))
    missing = gw.get("/gpu/v1/runs/999999/tasks", headers=auth(token))
    assert other.status_code == missing.status_code == 401 and other.text == missing.text
    assert gw.get(path, headers=auth(token)).status_code == 200


def test_token_expirado_ou_corrida_fechada_nao_servem(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    path = f"/gpu/v1/runs/{run_id}/tasks"
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        assert run is not None
        run.token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert gw.get(path, headers=auth(token)).status_code == 401
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        assert run is not None
        run.token_expires_at = datetime.now(UTC) + timedelta(hours=1)
        run.status = "cancelled"
        session.commit()
    assert gw.get(path, headers=auth(token)).status_code == 401


def test_cancelar_a_corrida_revoga_o_token(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    with services.sessions() as session:
        control.set_state(session, "stopped", "teste")
        session.commit()
    tick.tick(services)
    assert gw.get(f"/gpu/v1/runs/{run_id}/tasks", headers=auth(token)).status_code == 401
    with services.sessions() as session:
        assert session.scalars(select(BatchRun.token_hash)).one() is None


def test_o_gateway_so_aceita_resultados_da_sua_corrida_e_com_limite(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    path = f"/gpu/v1/runs/{run_id}/results"
    foreign = json.dumps({"id": f"r{run_id + 1}:j1:0", "content": "x"}) + "\n"
    assert gw.post(path, content=foreign, headers=auth(token)).status_code == 400
    assert gw.post(path, content="isto não é json\n", headers=auth(token)).status_code == 400
    assert gw.post(path, content=json.dumps({"sem": "id"}), headers=auth(token)).status_code == 400
    big = ("x" * (gateway.MAX_CALL_BYTES + 1)).encode()
    assert gw.post(path, content=big, headers=auth(token)).status_code == 413
    with services.sessions() as session:  # nada disto ficou guardado
        assert session.scalars(select(BatchRun.results_gz)).one() is None
    assert gw.post(path, content=b"", headers=auth(token)).status_code == 200  # vazio é inócuo
    bad_beat = gw.post(
        f"/gpu/v1/runs/{run_id}/heartbeat", json={"done": -1, "total": 1}, headers=auth(token)
    )
    assert bad_beat.status_code == 422


def test_limite_de_pedidos_por_corrida(lab: tuple[Services, FakeWiki, FakeGpuBackend]) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    with TestClient(create_gateway_app(services.sessions, rate_limit=3)) as limited:
        codes = [
            limited.get(f"/gpu/v1/runs/{run_id}/tasks", headers=auth(token)).status_code
            for _ in range(5)
        ]
    assert codes == [200, 200, 200, 429, 429]


def test_a_superficie_exposta_sao_estas_quatro_rotas_e_mais_nenhuma(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    app = gw.app
    routes = {
        (route.path, method)  # type: ignore[attr-defined]
        for route in app.routes  # type: ignore[attr-defined]
        for method in getattr(route, "methods", None) or ()
        if method != "HEAD"
    }
    assert routes == {
        ("/gpu/v1/runs/{run_id}/tasks", "GET"),
        ("/gpu/v1/runs/{run_id}/results", "POST"),
        ("/gpu/v1/runs/{run_id}/heartbeat", "POST"),
        ("/gpu/v1/runs/{run_id}/finish", "POST"),
    }
    for path in ("/docs", "/openapi.json", "/redoc", "/api/compute", "/health", "/"):
        assert gw.get(path).status_code == 404


# --- o pacote do Colab com túnel ----------


def test_pacote_magro_e_token_novo_a_cada_descarga(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient, database_url: str
) -> None:
    import io
    import zipfile

    from fastapi.testclient import TestClient as Client

    from ariadne.app import create_app
    from ariadne.auth import PrincipalKind, Role, create_principal

    services, _wiki, _gpu = lab
    services.settings.gpu_gateway_public_url = PUBLIC_URL
    seed(services, MALICIOSO)
    colab = services.get_backends()["colab"]
    with services.sessions() as session:
        run = runs.create_run(
            services, session, backend="colab", kinds=["curate_page"], limit=5,
            model="m", requested_by="1", manual=True, deadline_seconds=3600,
        )  # fmt: skip
        assert run is not None
        run.status = "awaiting_results"
        run_id = run.id
        _principal, key = create_principal(session, PrincipalKind.user, "pessoa", Role.admin)
        session.commit()
    assert colab.mode == "manual"

    app = create_app(services.settings.model_copy(update={"database_url": database_url}))
    with Client(app, headers={"Authorization": f"Bearer {key}"}) as api:
        tokens = []
        for _ in range(2):
            package = api.get(f"/api/compute/runs/{run_id}/package")
            assert package.status_code == 200
            archive = zipfile.ZipFile(io.BytesIO(package.content))
            assert archive.namelist() == ["manifest.json"]  # magro: os pedidos vêm pelo túnel
            tokens.append(json.loads(archive.read("manifest.json"))["gateway"]["token"])
    first, second = tokens
    path = f"/gpu/v1/runs/{run_id}/tasks"
    assert gw.get(path, headers=auth(first)).status_code == 401  # a descarga nova invalidou-o
    assert gw.get(path, headers=auth(second)).status_code == 200


# --- o papel Postgres limitado ----------


def test_o_papel_do_gateway_so_alcanca_o_batch_run(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], database_url: str
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    # `lab` usa a sua própria base: o papel é testado contra ela.
    url = make_url(database_url)  # a mesma base que o `lab` usa
    admin = url.set(drivername="postgresql").render_as_string(hide_password=False)
    role = f"ariadne_gwtest_{secrets.token_hex(3)}"
    password = secrets.token_hex(8)
    sql = (
        Path(__file__).resolve().parents[2] / "deploy" / "postgres" / "gpu-gateway.sql"
    ).read_text()
    sql = (
        "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        .replace("ariadne_gateway", role)
        .replace(":password", f"'{password}'")
        .replace(':"dbname"', f'"{url.database}"')
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql)  # type: ignore[arg-type]
    try:
        limited = make_engine(
            url.set(username=role, password=password).render_as_string(hide_password=False)
        )
        with TestClient(create_gateway_app(sessionmaker(limited))) as client:
            base = f"/gpu/v1/runs/{run_id}"
            assert client.get(f"{base}/tasks", headers=auth(token)).status_code == 200
            beat = client.post(
                f"{base}/heartbeat", json={"done": 0, "total": 1}, headers=auth(token)
            )
            assert beat.status_code == 200
            sent = client.post(f"{base}/results", content=results_of(gpu), headers=auth(token))
            assert sent.status_code == 200
            assert client.post(f"{base}/finish", json={}, headers=auth(token)).status_code == 200
        with limited.connect() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql("SELECT count(*) FROM principal")
        with limited.connect() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql("SELECT count(*) FROM curation")
        with limited.connect() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql("UPDATE batch_run SET backend = 'x'")
        limited.dispose()
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP OWNED BY "{role}"')  # type: ignore[arg-type]
            conn.execute(f'DROP ROLE "{role}"')  # type: ignore[arg-type]
    tick.tick(services)  # o que o gateway fechou importa-se como sempre
    assert curation_of(services, MALICIOSO).decision == "needs_review"
    assert isinstance(Job, type) and queue.BATCHED  # (símbolos usados nas asserções acima)
    _ = (Curation, Session)


# --- o que se rejeita antes de ir à base (revisão de segurança) ----------


def test_corpos_grandes_sao_cortados_antes_da_autenticacao(gw: TestClient) -> None:
    big = b"x" * (gateway.MAX_CALL_BYTES + 1)
    for route in ("heartbeat", "finish", "results"):
        assert gw.post(f"/gpu/v1/runs/1/{route}", content=big).status_code == 413

    def chunks() -> Iterator[bytes]:  # sem Content-Length: corta-se enquanto chega
        for _ in range(gateway.MAX_CALL_BYTES // (1024 * 1024) + 2):
            yield b"x" * (1024 * 1024)

    assert gw.post("/gpu/v1/runs/1/heartbeat", content=chunks()).status_code == 413


def test_falhas_a_mais_de_um_ip_levam_429_sem_ir_a_base(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    path = f"/gpu/v1/runs/{run_id}/tasks"
    attacker = {"CF-Connecting-IP": ip(203, 0, 113, 9), **auth("gpugw_errado")}
    codes = [gw.get(path, headers=attacker).status_code for _ in range(gateway.FAILURES_PER_IP + 2)]
    assert codes[: gateway.FAILURES_PER_IP] == [401] * gateway.FAILURES_PER_IP
    assert codes[-2:] == [429, 429]
    # o notebook, noutro IP, continua a passar
    notebook = {"CF-Connecting-IP": ip(198, 51, 100, 7), **auth(token)}
    assert gw.get(path, headers=notebook).status_code == 200


def test_ids_absurdos_e_tokens_sem_prefixo_dao_401(gw: TestClient) -> None:
    assert gw.get(f"/gpu/v1/runs/{2**70}/tasks", headers=auth("gpugw_x")).status_code == 401
    assert gw.get("/gpu/v1/runs/0/tasks", headers=auth("gpugw_x")).status_code == 401
    assert gw.get("/gpu/v1/runs/1/tasks", headers=auth("outro")).status_code == 401


def test_blocos_sem_fim_de_linha_e_resultados_repetitivos(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], gw: TestClient
) -> None:
    services, _wiki, gpu = lab
    run_id, token = launch(services, gpu)
    path = f"/gpu/v1/runs/{run_id}/results"
    one = json.dumps({"id": f"r{run_id}:j1:0", "content": "a b"}, ensure_ascii=False)
    two = json.dumps({"id": f"r{run_id}:j1:1", "content": "c"})
    assert gw.post(path, content=one.encode(), headers=auth(token)).json() == {"accepted": 1}
    assert gw.post(path, content=two.encode(), headers=auth(token)).json() == {"accepted": 1}
    with services.sessions() as session:
        stored = session.scalars(select(BatchRun.results_gz)).one()
        assert stored is not None
        replies, ignored = runs.parse_results(runs.unpack(stored), run_id)
    assert ignored == 0 and replies[1]["0"].content == "a b" and replies[1]["1"].content == "c"
    bomb = (two + "\n").encode() * 20_000  # ~800 KB que comprimem para quase nada
    assert gw.post(path, content=bomb, headers=auth(token)).status_code == 400


def test_unpack_com_limite_e_linhas_sem_separadores_unicode() -> None:
    data = runs.pack(b"a" * 1000) + runs.pack(b"b" * 1000)
    assert runs.unpack(data, 5000) == b"a" * 1000 + b"b" * 1000
    assert runs.unpack(data, 1500) == b"a" * 1000 + b"b" * 500
    line = runs.jsonl_line({"t": "x y\x85z "})
    assert len(line.decode().splitlines()) == 1
    assert json.loads(line)["t"] == "x y\x85z "


def test_finish_numa_corrida_ja_fechada_nao_a_ressuscita(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    run_id, _token = launch(services, gpu)
    # a corrida é cancelada depois de o gateway a autorizar: o UPDATE condicional recusa
    with services.sessions() as session:
        session.execute(update(BatchRun).where(BatchRun.id == run_id).values(status="cancelled"))
        session.commit()
        with pytest.raises(HTTPException) as caught:
            gateway._update_open(session, run_id, {"status": "importing"})
        assert caught.value.status_code == 409
    with services.sessions() as session:
        assert session.scalars(select(BatchRun.status)).one() == "cancelled"
