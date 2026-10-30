"""O destino Kaggle com uma CLI de brincar: os comandos, os ficheiros e o tratamento de erros."""

import json
import subprocess
from pathlib import Path

import pytest

from ariadne.compute.backends import BackendError, RunSpec
from ariadne.compute.kaggle import KERNEL_SLUG, KaggleBackend
from ariadne.config import Settings

TOKEN = "segredo-do-token-123"


class FakeCli:
    """Regista as chamadas à CLI e responde como o Kaggle responderia."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.files: dict[str, str] = {}  # o que havia nas pastas no momento da chamada
        self.kernel_status = 'has status "KernelWorkerStatus.RUNNING"'
        self.dataset_status = ["pending", "ready"]
        self.fail: dict[str, str] = {}
        self.output_files: dict[str, bytes] = {}

    def __call__(
        self, args: list[str], cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.envs.append(env)
        verb = " ".join(args[1:3])
        if verb in self.fail:
            return subprocess.CompletedProcess(args, 1, "", self.fail[verb])
        if verb == "datasets create":
            folder = Path(args[args.index("-p") + 1])
            for name in ("tasks.jsonl", "manifest.json", "dataset-metadata.json"):
                self.files[name] = (folder / name).read_text()
            return subprocess.CompletedProcess(args, 0, "Dataset creation error: no", "")
        if verb == "datasets status":
            state = (
                self.dataset_status.pop(0)
                if len(self.dataset_status) > 1
                else self.dataset_status[0]
            )
            return subprocess.CompletedProcess(args, 0, state, "")
        if verb == "kernels push":
            folder = Path(args[args.index("-p") + 1])
            self.files["kernel-metadata.json"] = (folder / "kernel-metadata.json").read_text()
            self.files["notebook"] = ",".join(sorted(p.name for p in folder.iterdir()))
            return subprocess.CompletedProcess(args, 0, "Kernel version 1 successfully pushed.", "")
        if verb == "kernels status":
            return subprocess.CompletedProcess(args, 0, self.kernel_status, "")
        if verb == "kernels output":
            target = Path(args[args.index("-p") + 1])
            for name, data in self.output_files.items():
                (target / name).write_bytes(data)
            return subprocess.CompletedProcess(args, 0, "Output file downloaded", "")
        return subprocess.CompletedProcess(args, 0, "ok", "")


def backend(cli: FakeCli, **settings: object) -> KaggleBackend:
    base = {"kaggle_username": "dono", "kaggle_api_token": TOKEN}
    return KaggleBackend(
        Settings(database_url="postgresql+psycopg://x@localhost/x", **{**base, **settings}),  # type: ignore[arg-type]
        runner=cli,
        sleep=lambda _s: None,
    )


def spec() -> RunSpec:
    return RunSpec(
        7,
        b'{"id": "r7:j1:0"}\n',
        {"run_id": 7, "model": "m"},
        3600,
        {"accelerator": "NvidiaTeslaT4"},
    )


def test_submit_cria_o_dataset_privado_e_lanca_o_notebook() -> None:
    cli = FakeCli()
    ref = backend(cli).submit(spec())
    assert ref == {
        "owner": "dono",
        "dataset": "ariadne-batch-7",
        "kernel": f"dono/{KERNEL_SLUG}",
        "version": 1,
    }
    verbs = [" ".join(c[1:3]) for c in cli.calls]
    assert verbs == ["datasets create", "datasets status", "datasets status", "kernels push"]
    assert "--public" not in cli.calls[0] and "-u" not in cli.calls[0]  # privado por omissão
    assert cli.files["tasks.jsonl"] == '{"id": "r7:j1:0"}\n'
    assert json.loads(cli.files["manifest.json"])["model"] == "m"
    metadata = json.loads(cli.files["kernel-metadata.json"])
    assert metadata["enable_gpu"] == "true" and metadata["is_private"] == "true"
    assert metadata["dataset_sources"] == ["dono/ariadne-batch-7"]
    assert metadata["id"] == f"dono/{KERNEL_SLUG}"
    assert cli.files["notebook"] == "ariadne_batch.ipynb,kernel-metadata.json"
    push = cli.calls[-1]
    assert push[push.index("--accelerator") + 1] == "NvidiaTeslaT4"
    assert push[push.index("-t") + 1] == "3600"
    # As credenciais vão no ambiente do processo, nunca na linha de comandos.
    assert all(TOKEN not in " ".join(c) for c in cli.calls)
    assert cli.envs[0]["KAGGLE_API_TOKEN"] == TOKEN and cli.envs[0]["KAGGLE_USERNAME"] == "dono"


def test_sem_credenciais_nao_arranca() -> None:
    cli = FakeCli()
    b = backend(cli, kaggle_api_token="", kaggle_key="")
    assert not b.configured() and "token" in " ".join(b.missing())
    with pytest.raises(BackendError, match="Missing"):
        b.submit(spec())
    assert cli.calls == []


def test_credenciais_dizem_so_se_estao_definidas() -> None:
    b = backend(FakeCli(), kaggle_key="chave-legada")
    shown = json.dumps(b.credentials())
    assert TOKEN not in shown and "chave-legada" not in shown
    assert {c["name"]: c["set"] for c in b.credentials()}["KAGGLE_USERNAME"] is True
    assert b.configured()


def test_erro_da_cli_nao_vaza_segredos() -> None:
    cli = FakeCli()
    cli.fail["kernels push"] = f"401 Unauthorized token={TOKEN}"
    with pytest.raises(BackendError) as info:
        backend(cli).submit(spec())
    assert "401" in str(info.value) and TOKEN not in str(info.value)


def test_dataset_que_nunca_fica_pronto() -> None:
    cli = FakeCli()
    cli.dataset_status = ["pending"]
    with pytest.raises(BackendError, match="was not ready"):
        backend(cli).submit(spec())
    assert not any(c[1:3] == ["kernels", "push"] for c in cli.calls)


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ('has status "KernelWorkerStatus.QUEUED"', "queued"),
        ('has status "KernelWorkerStatus.RUNNING"', "running"),
        ('has status "KernelWorkerStatus.COMPLETE"', "complete"),
        ('has status "KernelWorkerStatus.ERROR"', "failed"),
        ('has status "KernelWorkerStatus.CANCEL_ACKNOWLEDGED"', "failed"),
    ],
)
def test_estados_do_kernel(text: str, state: str) -> None:
    cli = FakeCli()
    cli.kernel_status = text
    assert backend(cli).poll({"kernel": "dono/ariadne-batch"}).state == state


def test_fetch_le_o_resultado_e_o_meta() -> None:
    cli = FakeCli()
    cli.output_files = {
        "results.jsonl": b'{"id": "r7:j1:0", "content": "x"}\n',
        "results.meta.json": b'{"run_id": 7, "gpu_seconds": 123.5}',
        "ruido.log": b"...",
    }
    fetched = backend(cli).fetch({"kernel": "dono/ariadne-batch"})
    assert fetched.results_jsonl.startswith(b'{"id": "r7')
    assert fetched.meta == {"run_id": 7, "gpu_seconds": 123.5}


def test_fetch_sem_results_falha() -> None:
    cli = FakeCli()
    cli.output_files = {"outro.txt": b"x"}
    with pytest.raises(BackendError, match=r"results\.jsonl"):
        backend(cli).fetch({"kernel": "dono/ariadne-batch"})


def test_cli_em_falta_da_um_erro_claro() -> None:
    def missing(
        args: list[str], cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(args[0])

    b = KaggleBackend(
        Settings(
            database_url="postgresql+psycopg://x@localhost/x",
            kaggle_username="dono",
            kaggle_api_token="t",
        ),
        runner=missing,
    )
    with pytest.raises(BackendError, match="is not installed"):
        b.poll({"kernel": "dono/ariadne-batch"})


def test_cleanup_apaga_o_dataset_e_ignora_falhas() -> None:
    cli = FakeCli()
    cli.fail["datasets delete"] = "not found"
    backend(cli).cleanup({"owner": "dono", "dataset": "ariadne-batch-7"})  # não levanta
    assert cli.calls[0][1:5] == ["datasets", "delete", "dono/ariadne-batch-7", "-y"]


def test_estado_e_output_usam_a_versao_do_kernel_com_plano_b() -> None:
    cli = FakeCli()
    b = backend(cli)
    ref = b.submit(spec())
    cli.calls.clear()
    b.poll(ref)
    assert cli.calls[-1][1:4] == ["kernels", "status", f"dono/{KERNEL_SLUG}/1"]
    cli.output_files = {"results.jsonl": b"{}\n"}
    b.fetch(ref)
    assert cli.calls[-1][3] == f"dono/{KERNEL_SLUG}/1"

    cli.fail["kernels status"] = "version not found"  # se a CLI não aceitar a versão
    with pytest.raises(BackendError):  # (o plano B também falha aqui: o erro vem à tona)
        b.poll(ref)
    assert [c[3] for c in cli.calls if c[1:3] == ["kernels", "status"]][-2:] == [
        f"dono/{KERNEL_SLUG}/1",
        f"dono/{KERNEL_SLUG}",
    ]
