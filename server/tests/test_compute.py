"""Controlo da curadoria e lotes em GPU externa (destino simulado)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariadne import queue
from ariadne.compute import control, quota, runs, tasks, tick
from ariadne.compute.runs import RunError
from ariadne.curation import queue_view
from ariadne.db.models import BatchRun, BatchTask, Curation, Job
from ariadne.services import Services
from ariadne.workers.runner import Runner
from tests.curationzims import MALICIOSO, REMEDIO, make_dir
from tests.gpufakes import FakeGpuBackend
from tests.test_curation import (
    PREFIX,
    build,
    curation_of,
    request_import,
    run_jobs,
)
from tests.wikifakes import FakeCurator, FakeWiki

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("short_articles")]


@pytest.fixture
def zims(tmp_path: Path) -> Path:
    return make_dir(tmp_path / "zims")


@pytest.fixture
def curator() -> Iterator[FakeCurator]:
    fake = FakeCurator()
    yield fake
    fake.stop()


@pytest.fixture
def lab(
    db: sessionmaker[Session], zims: Path, curator: FakeCurator
) -> Iterator[tuple[Services, FakeWiki, FakeGpuBackend]]:
    """Um curador local e um «Kaggle» de brincar (com outro curador falso, o do kernel)."""
    services, wiki = build(db, zims, curator_url=curator.url, curator_model="local-falso")
    remote = FakeCurator()
    gpu = FakeGpuBackend(remote)
    services.backends = {**services.get_backends(), "kaggle": gpu}
    yield services, wiki, gpu
    remote.stop()
    services.close()


def pending_curation_jobs(services: Services) -> list[Job]:
    with services.sessions() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "curate_page").order_by(Job.id)).all()
        session.expunge_all()
        return list(jobs)


def seed(services: Services, *ids: str) -> None:
    """Importa os documentos com os workers `importer` (sem tocar no curador): ficam os jobs
    `curate_page` à espera."""
    request_import(services, *ids)
    run_jobs(services, ("importer",))


def set_panel(services: Services, **changes: object) -> None:
    with services.sessions() as session:
        control.update(session, "teste", **changes)
        session.commit()


def curate_runner(services: Services) -> Runner:
    return Runner(services, ["curator"])


# --- janela e estado -------------------------------------------------------------------------


def make_control(**fields: object) -> object:
    from ariadne.db.models import ComputeControl

    base = {
        "state": "running",
        "schedule_enabled": True,
        "window_start": "23:00",
        "window_end": "07:00",
        "timezone": "UTC",
        "manual_override": False,
    }
    return ComputeControl(**{**base, **fields})  # type: ignore[arg-type]


def lisbon(hour: int, minute: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("UTC")).astimezone(UTC)


def test_janela_que_passa_a_meia_noite() -> None:
    c = make_control()
    assert control.in_window(c, lisbon(23, 30))  # type: ignore[arg-type]
    assert control.in_window(c, lisbon(3))  # type: ignore[arg-type]
    assert not control.in_window(c, lisbon(7))  # type: ignore[arg-type]
    assert not control.in_window(c, lisbon(15))  # type: ignore[arg-type]
    assert control.window_remaining(c, lisbon(23, 30)) == pytest.approx(7.5 * 3600)  # type: ignore[arg-type]
    assert control.window_remaining(c, lisbon(6)) == pytest.approx(3600)  # type: ignore[arg-type]
    assert control.window_remaining(c, lisbon(12)) == 0  # type: ignore[arg-type]


def test_janela_no_mesmo_dia_e_sem_janela() -> None:
    day = make_control(window_start="09:00", window_end="17:00")
    assert control.in_window(day, lisbon(12)) and not control.in_window(day, lisbon(18))  # type: ignore[arg-type]
    free = make_control(schedule_enabled=False)
    assert control.window_remaining(free, lisbon(12)) is None  # type: ignore[arg-type]
    assert control.in_window(free, lisbon(12))  # type: ignore[arg-type]


def test_ativo_precisa_de_play_e_janela_ou_arranque_manual() -> None:
    now = lisbon(15)
    assert not control.is_active(make_control(), now)  # type: ignore[arg-type]
    assert control.is_active(make_control(manual_override=True), now)  # type: ignore[arg-type]
    assert not control.is_active(make_control(state="paused", manual_override=True), now)  # type: ignore[arg-type]
    assert control.is_active(make_control(), lisbon(2))  # type: ignore[arg-type]


def test_valores_invalidos_do_painel(db: sessionmaker[Session]) -> None:
    with db() as session:
        with pytest.raises(control.ControlError):
            control.update(session, "x", window_start="25:99")
        with pytest.raises(control.ControlError):
            control.update(session, "x", timezone="Marte/Olympus")
        with pytest.raises(control.ControlError):
            control.update(session, "x", disabled_kinds=["nao_existe"])
        with pytest.raises(control.ControlError):
            control.set_state(session, "dormir", "x")


# --- o curador local obedece ao painel ---------------------------------------------------------


def test_por_omissao_o_local_trabalha_como_antes(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, _gpu = lab
    services.settings.batch_auto_publish = True  # aqui não se testa o portão da revisão
    request_import(services, MALICIOSO)
    run_jobs(services)
    assert curation_of(services, MALICIOSO).decision == "published"


def test_pausa_retoma_e_janela_governam_o_local(
    lab: tuple[Services, FakeWiki, FakeGpuBackend], curator: FakeCurator
) -> None:
    services, _wiki, _gpu = lab
    services.settings.batch_auto_publish = True  # aqui não se testa o portão da revisão
    seed(services, MALICIOSO)
    with services.sessions() as session:
        control.set_state(session, "paused", "teste")
        session.commit()
    assert run_jobs(services, ("curator",)) == 0 and curator.requests == []
    assert curation_of(services, MALICIOSO).decision == "pending_curator"

    # Retomar fora da janela: continua parado; com arranque manual, corre.
    set_panel(services, schedule_enabled=True, window_start="03:00", window_end="03:01")
    with services.sessions() as session:
        control.set_state(session, "running", "teste")
        session.commit()
    assert run_jobs(services, ("curator",)) == 0
    with services.sessions() as session:
        control.set_state(session, "running", "teste", ignore_window=True)
        session.commit()
    assert run_jobs(services, ("curator",)) == 1
    assert curation_of(services, MALICIOSO).decision == "published"


# --- lote ponta a ponta ------------------------------------------------------------------------


def go_external(services: Services, **extra: object) -> None:
    set_panel(services, destination="kaggle", **extra)


def test_kaggle_ponta_a_ponta_e_o_resultado_fica_para_revisao(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, wiki, gpu = lab
    seed(services, MALICIOSO)
    go_external(services)

    tick.tick(services)  # lança a corrida
    [spec] = gpu.submitted
    assert spec.manifest["model"] == "modelo-falso" and spec.deadline_seconds <= 9 * 3600
    with services.sessions() as session:
        run = runs.current_run(session)
        assert run is not None and run.status == "queued" and run.task_count == 1
        assert run.request_count >= 1
    [job] = pending_curation_jobs(services)
    assert job.status == "batched"
    assert run_jobs(services, ("curator",)) == 0  # o local não lhe toca: «primeiro lá»

    tick.tick(services)  # queued -> running
    with services.sessions() as session:
        assert session.scalars(select(BatchRun.status)).one() == "running"
    tick.tick(services)  # complete -> recolhe e importa
    with services.sessions() as session:
        run = session.scalars(select(BatchRun)).one()
        assert run.status == "done" and run.review_count == 1 and run.gpu_seconds == 1800
        assert run.tasks_gz is None and run.results_gz is None
        assert session.scalars(select(BatchTask.status)).one() == "review"
    assert gpu.cleaned
    [job] = pending_curation_jobs(services)
    assert job.status == "done"
    row = curation_of(services, MALICIOSO)
    # Fiel ao original, mas veio de fora: fica para o dono validar, não se publica.
    assert row.decision == "needs_review" and row.batch_run_id == run.id
    assert row.fidelity is not None and row.fidelity["ok"] is True
    assert "external batch" in (row.reason or "")
    assert f"{PREFIX}/malicioso" not in wiki.pages

    with services.sessions() as session:  # a validação humana publica
        queue_view.review_item(
            session,
            MALICIOSO,
            approve=True,
            reviewer="1",
            note=None,
            fingerprint=row.fingerprint,
        )
        session.commit()
    run_jobs(services, ("importer",))
    assert "Resumo de referência." in wiki.pages[f"{PREFIX}/malicioso"].content


def test_auto_publicacao_so_com_a_flag(lab: tuple[Services, FakeWiki, FakeGpuBackend]) -> None:
    services, _wiki, _gpu = lab
    services.settings.batch_auto_publish = True
    seed(services, MALICIOSO)
    go_external(services)
    for _ in range(3):
        tick.tick(services)
    assert curation_of(services, MALICIOSO).decision == "published"


def test_a_sobra_passa_para_o_local(lab: tuple[Services, FakeWiki, FakeGpuBackend]) -> None:
    services, _wiki, gpu = lab
    services.settings.batch_auto_publish = True  # aqui não se testa o portão da revisão
    seed(services, MALICIOSO)
    gpu.drop = {"0"}  # o notebook não chegou a este pedido
    go_external(services)
    for _ in range(3):
        tick.tick(services)
    with services.sessions() as session:
        run = session.scalars(select(BatchRun)).one()
        assert run.status == "done" and run.applied_count + run.review_count == 0
        assert run.invalid_count == 1 and run.released_count == 1
    [job] = pending_curation_jobs(services)
    assert job.status == "pending" and job.payload.get(queue.BATCH_TRIED) is True
    # Agora o local trata dele (já passou pelo lote) mesmo com o destino externo ativo.
    assert run_jobs(services, ("curator",)) == 1
    assert curation_of(services, MALICIOSO).decision == "published"


def test_resposta_invalida_do_lote_nao_publica_e_volta_ao_local(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    gpu.corrupt = {"0"}
    go_external(services)
    for _ in range(3):
        tick.tick(services)
    with services.sessions() as session:
        task = session.scalars(select(BatchTask)).one()
        assert task.status == "invalid" and "JSON" in (task.detail or "")
    assert curation_of(services, MALICIOSO).decision == "pending_curator"


def test_primeiro_la_o_local_espera_ate_o_destino_bloquear(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    services.settings.batch_auto_publish = True  # aqui não se testa o portão da revisão
    seed(services, MALICIOSO)
    gpu.is_configured = False  # sem credenciais
    go_external(services)
    set_panel(services, state=None)
    tick.tick(services)
    with services.sessions() as session:
        assert "missing credentials" in (control.get_control(session).external_blocked_reason or "")
        assert runs.current_run(session) is None
    # Bloqueado: o local fica com o trabalho (se `fallback_local`).
    assert run_jobs(services, ("curator",)) == 1
    assert curation_of(services, MALICIOSO).decision == "published"


def test_sem_reserva_local_nada_corre_quando_o_destino_bloqueia(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    gpu.is_configured = False
    go_external(services, fallback_local=False)
    tick.tick(services)
    assert run_jobs(services, ("curator",)) == 0


def test_pausa_nao_lanca_corridas_novas(lab: tuple[Services, FakeWiki, FakeGpuBackend]) -> None:
    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    go_external(services)
    with services.sessions() as session:
        control.set_state(session, "paused", "teste")
        session.commit()
    tick.tick(services)
    assert gpu.submitted == []
    with services.sessions() as session:
        control.set_state(session, "running", "teste")
        session.commit()
    tick.tick(services)
    assert len(gpu.submitted) == 1


def test_fora_da_janela_nao_lanca_e_o_prazo_cabe_na_janela(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    go_external(services, schedule_enabled=True, window_start="23:00", window_end="07:00")
    tick.tick(services, lisbon(15))
    assert gpu.submitted == []
    tick.tick(services, lisbon(5))  # faltam 2 h para fechar
    [spec] = gpu.submitted
    assert 3600 < spec.deadline_seconds <= 2 * 3600


def test_quota_esgotada_bloqueia_e_abre_o_local(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    go_external(services)
    now = datetime.now(UTC)
    with services.sessions() as session:
        cfg = {
            "kaggle": {
                "weekly_hours": 30,
                "used_hours_baseline": 29.9,
                "used_hours_baseline_at": now.isoformat(),
            }
        }
        control.get_control(session).destinations = cfg
        session.commit()
    tick.tick(services, now + timedelta(seconds=5))
    assert gpu.submitted == []
    with services.sessions() as session:
        assert "quota" in (control.get_control(session).external_blocked_reason or "")
    assert run_jobs(services, ("curator",)) == 1


def test_calculo_da_quota(db: sessionmaker[Session]) -> None:
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)  # quarta-feira
    assert quota.period_start(now, 5) == datetime(2026, 11, 7, tzinfo=UTC)  # sábado anterior
    with db() as session:
        session.add(
            BatchRun(
                backend="kaggle",
                status="done",
                running_at=now - timedelta(days=1),
                gpu_seconds=3.5 * 3600,
            )
        )
        session.add(
            BatchRun(
                backend="kaggle",
                status="done",
                running_at=now - timedelta(days=9),
                gpu_seconds=9 * 3600,
            )
        )
        session.flush()
        q = quota.compute_quota(session, "kaggle", {"weekly_hours": 30}, now)
        assert q is not None and q.used_hours == pytest.approx(3.5)
        assert q.remaining_hours == pytest.approx(26.5)
        assert q.resets_at == datetime(2026, 11, 14, tzinfo=UTC)
        base = {
            "weekly_hours": 30,
            "used_hours_baseline": 20,
            "used_hours_baseline_at": now.isoformat(),
        }
        q2 = quota.compute_quota(session, "kaggle", base, now)
        assert q2 is not None and q2.remaining_hours == pytest.approx(10)
        assert quota.compute_quota(session, "kaggle", {}, now) is None


def test_falhas_seguidas_bloqueiam_o_destino(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    gpu.fail_submit = "kaggle recusou"
    go_external(services)
    t = datetime.now(UTC)
    for n in range(4):  # a 4.ª volta já vê as 3 falhas
        tick.tick(services, t + timedelta(minutes=15 * n))
    with services.sessions() as session:
        statuses = session.scalars(select(BatchRun.status).order_by(BatchRun.id)).all()
        assert statuses == ["failed"] * 3
        assert "failed" in (control.get_control(session).external_blocked_reason or "")
    [job] = pending_curation_jobs(services)
    assert job.status == "pending" and queue.BATCH_TRIED not in job.payload
    assert run_jobs(services, ("curator",)) == 1  # o local assume


def test_parar_cancela_e_devolve_as_tarefas(lab: tuple[Services, FakeWiki, FakeGpuBackend]) -> None:
    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    go_external(services)
    tick.tick(services)
    with services.sessions() as session:
        control.set_state(session, "stopped", "teste")
        session.commit()
    tick.tick(services)
    with services.sessions() as session:
        assert session.scalars(select(BatchRun.status)).one() == "cancelled"
    assert gpu.cancelled
    [job] = pending_curation_jobs(services)
    assert job.status == "pending"


def test_o_destino_nunca_responde_e_a_corrida_expira(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    gpu.states = ["running"]
    go_external(services)
    tick.tick(services)
    later = datetime.now(UTC) + timedelta(hours=10)
    tick.tick(services, later)
    with services.sessions() as session:
        run = session.scalars(select(BatchRun)).one()
        assert run.status == "failed" and "in time" in (run.error or "")
    [job] = pending_curation_jobs(services)
    assert job.status == "pending" and job.payload.get(queue.BATCH_TRIED) is True


# --- lote manual (Colab) e conteúdo privado ------------------------------------------------------


def test_lote_manual_exporta_e_importa_um_ficheiro(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    import io
    import json
    import zipfile

    services, _wiki, _gpu = lab
    seed(services, MALICIOSO)
    colab = services.get_backends()["colab"]
    with services.sessions() as session:
        run = runs.create_run(
            services,
            session,
            backend="colab",
            kinds=["curate_page"],
            limit=10,
            model="m",
            requested_by="1",
            manual=True,
            deadline_seconds=None,
        )
        assert run is not None
        run.status = "awaiting_results"
        pack = runs.bundle(run, runs.build_manifest(run, 3600, colab.default_config()))
        run_id = run.id
        session.commit()
    names = zipfile.ZipFile(io.BytesIO(pack)).namelist()
    assert sorted(names) == ["manifest.json", "tasks.jsonl"]
    tasks_file = zipfile.ZipFile(io.BytesIO(pack)).read("tasks.jsonl").decode().splitlines()
    request = json.loads(tasks_file[0])
    assert set(request) == {"id", "kind", "prompt_version", "messages", "max_tokens", "temperature"}
    assert request["id"].startswith(f"r{run_id}:j")

    remote = FakeCurator()
    try:
        lines = [
            json.dumps(
                {
                    "id": json.loads(t)["id"],
                    "content": remote.reply({"messages": json.loads(t)["messages"]}),
                }
            )
            for t in tasks_file
        ]
    finally:
        remote.stop()
    with services.sessions() as session:  # um ficheiro de outra corrida não serve
        with pytest.raises(RunError):
            runs.store_results(
                session, session.get(BatchRun, run_id), b'{"id": "r999:j1:0", "content": "x"}\n'
            )  # type: ignore[arg-type]
        runs.store_results(session, session.get(BatchRun, run_id), "\n".join(lines).encode())  # type: ignore[arg-type]
        session.commit()
    tick.tick(services)
    assert curation_of(services, MALICIOSO).decision == "needs_review"
    with services.sessions() as session:
        assert session.scalars(select(BatchRun.status)).one() == "done"


def test_so_uma_corrida_de_cada_vez(lab: tuple[Services, FakeWiki, FakeGpuBackend]) -> None:
    services, _wiki, _gpu = lab
    seed(services, MALICIOSO, REMEDIO)
    with services.sessions() as session:
        runs.create_run(
            services,
            session,
            backend="colab",
            kinds=["curate_page"],
            limit=1,
            model=None,
            requested_by=None,
            manual=True,
            deadline_seconds=None,
        )
        session.commit()
    with services.sessions() as session, pytest.raises(RunError, match="in progress"):
        runs.create_run(
            services,
            session,
            backend="colab",
            kinds=["curate_page"],
            limit=1,
            model=None,
            requested_by=None,
            manual=True,
            deadline_seconds=None,
        )


def test_nao_cria_corrida_com_modelo_chines(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, _gpu = lab
    seed(services, MALICIOSO)
    with services.sessions() as session, pytest.raises(RunError, match="excluded from Ariadne"):
        runs.create_run(
            services,
            session,
            backend="kaggle",
            kinds=["curate_page"],
            limit=1,
            model="Qwen/Qwen2.5-7B-Instruct-AWQ",
            requested_by=None,
            manual=False,
            deadline_seconds=None,
        )
    with services.sessions() as session:
        assert session.scalars(select(BatchRun)).first() is None  # nem a corrida nem os jobs saem


def test_nada_para_enviar(lab: tuple[Services, FakeWiki, FakeGpuBackend]) -> None:
    services, _wiki, _gpu = lab
    with services.sessions() as session:
        run = runs.create_run(
            services,
            session,
            backend="colab",
            kinds=["curate_page"],
            limit=5,
            model=None,
            requested_by=None,
            manual=True,
            deadline_seconds=None,
        )
        assert run is None


def test_conteudo_privado_nao_sai_para_a_gpu_externa(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    from ariadne.db.models import Source

    services, _wiki, gpu = lab
    seed(services, MALICIOSO)
    with services.sessions() as session:
        document_source = session.scalars(select(Curation)).one().document_id.split("/")[0]
        source = session.get(Source, document_source)
        assert source is not None
        source.license_class = "private"
        session.commit()
    go_external(services)
    tick.tick(services)
    assert gpu.submitted == []  # nada seguiu
    [job] = pending_curation_jobs(services)
    assert job.status == "pending" and job.payload.get(queue.BATCH_TRIED) is True


def test_tipos_registados_e_ids() -> None:
    assert "curate_page" in tasks.kinds()
    assert tasks.parse_request_id(tasks.request_id(4, 12, "3")) == (4, 12, "3")
    assert tasks.parse_request_id("lixo") is None
    assert tasks.parse_request_id("r1:j2") is None


def test_cli_exportar_e_importar_um_lote(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import json
    import zipfile

    from typer.testing import CliRunner

    from ariadne import cli_batch
    from ariadne.cli import app as cli

    services, _wiki, _gpu = lab
    seed(services, MALICIOSO)
    monkeypatch.setattr(services, "close", lambda: None)  # a fixture é que fecha
    monkeypatch.setattr(cli_batch, "_services", lambda: services)
    runner = CliRunner()

    pack = tmp_path / "lote.zip"
    exported = runner.invoke(cli, ["batch", "export", str(pack), "--limit", "5"])
    assert exported.exit_code == 0, exported.output
    assert "1 tasks" in exported.output
    again = runner.invoke(cli, ["batch", "export", str(pack)])
    assert again.exit_code == 1 and "in progress" in again.output

    archive = zipfile.ZipFile(pack)
    remote = FakeCurator()
    try:
        results = "\n".join(
            json.dumps(
                {
                    "id": json.loads(t)["id"],
                    "content": remote.reply({"messages": json.loads(t)["messages"]}),
                }
            )
            for t in archive.read("tasks.jsonl").decode().splitlines()
        )
    finally:
        remote.stop()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("results.jsonl", results)
    file = tmp_path / "ariadne_results.zip"
    file.write_bytes(out.getvalue())

    imported = runner.invoke(cli, ["batch", "import", str(file)])
    assert imported.exit_code == 0, imported.output
    assert curation_of(services, MALICIOSO).decision == "needs_review"
    status = runner.invoke(cli, ["batch", "status"])
    assert status.exit_code == 0 and "curate_page" in status.output and "done" in status.output


def manual_run(services: Services, backend: str = "colab", deadline: int | None = 12 * 3600) -> int:
    with services.sessions() as session:
        run = runs.create_run(
            services, session, backend=backend, kinds=["curate_page"], limit=5,
            model=None, requested_by=None, manual=True, deadline_seconds=deadline,
        )  # fmt: skip
        assert run is not None
        run.status, run.submitted_at = "awaiting_results", datetime.now(UTC)
        run_id = run.id
        session.commit()
    return run_id


def test_corrida_manual_esquecida_expira_e_liberta_as_tarefas(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, _gpu = lab
    seed(services, MALICIOSO)
    run_id = manual_run(services)
    tick.tick(services, datetime.now(UTC) + timedelta(hours=2))  # ainda dentro do prazo
    with services.sessions() as session:
        assert session.get(BatchRun, run_id).status == "awaiting_results"  # type: ignore[union-attr]
    [job] = pending_curation_jobs(services)
    assert job.status == "batched"

    tick.tick(services, datetime.now(UTC) + timedelta(hours=13))  # 12 h + margem
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        assert run is not None and run.status == "failed" and "in time" in (run.error or "")
        assert runs.current_run(session) is None  # já não bloqueia as corridas seguintes
    [job] = pending_curation_jobs(services)
    assert job.status == "pending" and job.payload.get(queue.BATCH_TRIED) is True
    with services.sessions() as session:  # já não há «corrida em curso» a impedir outra
        again = runs.create_run(
            services, session, backend="colab", kinds=["curate_page"], limit=5,
            model=None, requested_by=None, manual=True, deadline_seconds=None,
        )  # fmt: skip
        assert again is None  # (só não há nada para enviar: o job já foi tentado)


def test_corrida_de_um_destino_que_ja_nao_existe_expira(
    lab: tuple[Services, FakeWiki, FakeGpuBackend],
) -> None:
    services, _wiki, _gpu = lab
    seed(services, MALICIOSO)
    run_id = manual_run(services, backend="desaparecido", deadline=3600)
    tick.tick(services, datetime.now(UTC) + timedelta(minutes=30))
    with services.sessions() as session:
        assert session.get(BatchRun, run_id).status == "awaiting_results"  # type: ignore[union-attr]
    tick.tick(services, datetime.now(UTC) + timedelta(hours=3))
    with services.sessions() as session:
        assert session.get(BatchRun, run_id).status == "failed"  # type: ignore[union-attr]
    [job] = pending_curation_jobs(services)
    assert job.status == "pending"


def test_zip_de_resultados_tem_teto_descomprimido(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("results.jsonl", b"0" * 5000)  # comprime a quase nada
    monkeypatch.setattr(runs, "MAX_RESULTS_UNZIPPED", 1000)
    with pytest.raises(ValueError, match="uncompressed"):
        runs.read_results_file(out.getvalue())
    monkeypatch.setattr(runs, "MAX_RESULTS_UNZIPPED", 10_000)
    data, meta = runs.read_results_file(out.getvalue())
    assert len(data) == 5000 and meta == {}
    assert runs.read_results_file(b'{"id": "x"}') == (b'{"id": "x"}', {})
