"""Destino Kaggle: GPU gratuita da Google, automático, sem expor o Ariadne à internet.

Tudo parte do Ariadne, pela CLI oficial do Kaggle (`kaggle`, extra `ariadne[kaggle]`):

1. `kaggle datasets create`: um dataset **privado** por corrida com `tasks.jsonl` e `manifest.json`;
2. `kaggle kernels push`: o notebook `deploy/gpu/ariadne_batch.ipynb` (GPU + internet ligada, só
   para instalar o vLLM e descarregar o modelo) com esse dataset como entrada;
3. `kaggle kernels status`: o estado da última corrida do notebook;
4. `kaggle kernels output`: o `results.jsonl` que o notebook escreveu.

O notebook nunca fala com o Ariadne: lê o dataset e escreve o resultado. Só saem do ambiente os
textos que o `prepare` de cada tipo de tarefa pôs no JSONL (nunca conteúdo privado).

Credenciais (variáveis de ambiente do worker): `KAGGLE_USERNAME` e `KAGGLE_API_TOKEN` (ou a chave
legada `KAGGLE_KEY`).
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ariadne.compute.backends import (
    BackendError,
    ComputeBackend,
    Fetched,
    Mode,
    PollResult,
    RunSpec,
    flag,
)
from ariadne.config import Settings

KERNEL_SLUG = "ariadne-batch"
NOTEBOOK_NAME = "ariadne_batch.ipynb"
DATASET_READY_SECONDS = 240.0
COMMAND_TIMEOUT = 600.0

# (argumentos, pasta de trabalho, ambiente) -> processo; os testes trocam-no por um falso.
Runner = Callable[[list[str], Path, dict[str, str]], subprocess.CompletedProcess[str]]


def _subprocess_runner(
    args: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False
    )


def notebook_path(settings: Settings) -> Path:
    candidates = [
        Path(settings.batch_notebook_path) if settings.batch_notebook_path else None,
        Path(__file__).resolve().parents[2] / "deploy" / "gpu" / NOTEBOOK_NAME,  # imagem: /app
        Path(__file__).resolve().parents[3] / "deploy" / "gpu" / NOTEBOOK_NAME,  # repositório
    ]
    for path in candidates:
        if path is not None and path.is_file():
            return path
    raise BackendError(
        f"Cannot find the notebook {NOTEBOOK_NAME} (set BATCH_NOTEBOOK_PATH or include "
        "deploy/gpu in the image)."
    )


class KaggleBackend(ComputeBackend):
    id = "kaggle"
    label = "Free Google GPU (Kaggle)"
    mode: Mode = "auto"
    description = (
        "Free Kaggle GPU (T4, about 30 h per week, sessions of a few hours). "
        "Automatic: Ariadne creates a private dataset, launches the notebook, watches it and "
        "imports the result. Only the JSONL files leave, through the Kaggle API; nothing needs "
        "to reach Ariadne."
    )

    def __init__(
        self,
        settings: Settings,
        runner: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._runner = runner or _subprocess_runner
        self._sleep = sleep

    # --- configuração -------------------------------------------------------------------------

    def credentials(self) -> list[dict[str, Any]]:
        s = self.settings
        token_or_key = bool(s.kaggle_api_token or s.kaggle_key)
        return [
            flag("KAGGLE_USERNAME", s.kaggle_username),
            {"name": "KAGGLE_API_TOKEN", "set": bool(s.kaggle_api_token), "required": False},
            {"name": "KAGGLE_KEY", "set": bool(s.kaggle_key), "required": False},
            {"name": "token or key (either one)", "set": token_or_key, "required": True},
        ]

    def default_config(self) -> dict[str, Any]:
        s = self.settings
        return {
            "model": s.kaggle_model,
            "accelerator": s.kaggle_accelerator,
            "weekly_hours": s.kaggle_weekly_gpu_hours,
            "session_hours": s.kaggle_session_hours,
            "reset_weekday": s.kaggle_quota_reset_weekday,
        }

    def _env(self, config_dir: Path) -> dict[str, str]:
        s = self.settings
        env = {k: v for k, v in os.environ.items() if not k.startswith("KAGGLE_")}
        env["KAGGLE_CONFIG_DIR"] = str(config_dir)
        env["KAGGLE_USERNAME"] = s.kaggle_username
        if s.kaggle_api_token:
            env["KAGGLE_API_TOKEN"] = s.kaggle_api_token
        if s.kaggle_key:
            env["KAGGLE_KEY"] = s.kaggle_key
        return env

    def _cli(self, work: Path, *args: str) -> str:
        """Corre `kaggle <args>`; levanta `BackendError` (sem segredos) se falhar."""
        config = work / ".kaggle"
        config.mkdir(exist_ok=True)
        command = [self.settings.kaggle_cli, *args]
        try:
            done = self._runner(command, work, self._env(config))
        except FileNotFoundError as exc:
            raise BackendError(
                f"The Kaggle CLI ({self.settings.kaggle_cli!r}) is not installed."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"kaggle {args[0]} {args[1]}: timed out") from exc
        out = f"{done.stdout or ''}\n{done.stderr or ''}".strip()
        if done.returncode != 0:
            raise BackendError(f"kaggle {' '.join(args[:2])}: {self._scrub(out)[:600]}")
        return out

    def _scrub(self, text: str) -> str:
        for secret in (self.settings.kaggle_api_token, self.settings.kaggle_key):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _owner(self) -> str:
        if not self.settings.kaggle_username:
            raise BackendError("KAGGLE_USERNAME is missing.")
        return self.settings.kaggle_username

    # --- ciclo de uma corrida -----------------------------------------------------------------

    def submit(self, spec: RunSpec) -> dict[str, Any]:
        if not self.configured():
            raise BackendError("Missing Kaggle credentials: " + ", ".join(self.missing()))
        owner = self._owner()
        dataset = f"ariadne-batch-{spec.run_id}"
        notebook = notebook_path(self.settings)
        with tempfile.TemporaryDirectory(prefix="ariadne-kaggle-") as tmp:
            work = Path(tmp)
            data = work / "dataset"
            kernel = work / "kernel"
            data.mkdir()
            kernel.mkdir()
            (data / "tasks.jsonl").write_bytes(spec.tasks_jsonl)
            (data / "manifest.json").write_text(json.dumps(spec.manifest), encoding="utf-8")
            (data / "dataset-metadata.json").write_text(
                json.dumps(
                    {
                        "title": f"Ariadne batch {spec.run_id}",
                        "id": f"{owner}/{dataset}",
                        "licenses": [{"name": "other"}],
                    }
                ),
                encoding="utf-8",
            )
            self._cli(work, "datasets", "create", "-p", str(data))
            self._wait_dataset(work, f"{owner}/{dataset}")
            shutil.copy(notebook, kernel / NOTEBOOK_NAME)
            (kernel / "kernel-metadata.json").write_text(
                json.dumps(
                    {
                        "id": f"{owner}/{KERNEL_SLUG}",
                        "title": KERNEL_SLUG,
                        "code_file": NOTEBOOK_NAME,
                        "language": "python",
                        "kernel_type": "notebook",
                        "is_private": "true",
                        "enable_gpu": "true",
                        "enable_internet": "true",
                        "dataset_sources": [f"{owner}/{dataset}"],
                        "competition_sources": [],
                        "kernel_sources": [],
                        "model_sources": [],
                    }
                ),
                encoding="utf-8",
            )
            accelerator = str(spec.config.get("accelerator") or self.settings.kaggle_accelerator)
            out = self._cli(
                work,
                "kernels",
                "push",
                "-p",
                str(kernel),
                "--accelerator",
                accelerator,
                "-t",
                str(spec.deadline_seconds),
            )
            if re.search(r"\berror\b", out, re.IGNORECASE) and "success" not in out.lower():
                raise BackendError(f"kaggle kernels push: {self._scrub(out)[:600]}")
        ref: dict[str, Any] = {
            "owner": owner,
            "dataset": dataset,
            "kernel": f"{owner}/{KERNEL_SLUG}",
        }
        # «Kernel version N successfully pushed»: com o N, o estado e o output são os desta corrida
        # e não os da anterior (o slug do kernel é sempre o mesmo).
        if (found := re.search(r"version\s+(\d+)", out, re.IGNORECASE)) is not None:
            ref["version"] = int(found.group(1))
        return ref

    def _kernel(self, ref: dict[str, Any], *, versioned: bool = True) -> str:
        if versioned and ref.get("version"):
            return f"{ref['kernel']}/{ref['version']}"
        return str(ref["kernel"])

    def _wait_dataset(self, work: Path, dataset: str) -> None:
        waited = 0.0
        while True:
            out = self._cli(work, "datasets", "status", dataset)
            if "ready" in out.lower():
                return
            if "error" in out.lower() or "fail" in out.lower():
                raise BackendError(f"dataset {dataset} failed: {self._scrub(out)[:300]}")
            if waited >= DATASET_READY_SECONDS:
                raise BackendError(f"dataset {dataset} was not ready in time")
            self._sleep(5.0)
            waited += 5.0

    def poll(self, ref: dict[str, Any]) -> PollResult:
        with tempfile.TemporaryDirectory(prefix="ariadne-kaggle-") as tmp:
            try:
                out = self._cli(Path(tmp), "kernels", "status", self._kernel(ref))
            except BackendError:
                if not ref.get("version"):
                    raise
                out = self._cli(Path(tmp), "kernels", "status", self._kernel(ref, versioned=False))
        text = out.lower()
        if "complete" in text:
            return PollResult("complete", out[-200:])
        if "cancel" in text or "error" in text or "fail" in text:
            return PollResult("failed", self._scrub(out)[-300:])
        if "running" in text:
            return PollResult("running", "")
        return PollResult("queued", "")

    def fetch(self, ref: dict[str, Any]) -> Fetched:
        with tempfile.TemporaryDirectory(prefix="ariadne-kaggle-") as tmp:
            work = Path(tmp)
            target = work / "output"
            target.mkdir()
            try:
                self._cli(work, "kernels", "output", self._kernel(ref), "-p", str(target), "-o")
            except BackendError:
                if not ref.get("version"):
                    raise
                self._cli(
                    work,
                    "kernels",
                    "output",
                    self._kernel(ref, versioned=False),
                    "-p",
                    str(target),
                    "-o",
                )
            results = next(target.rglob("results.jsonl"), None)
            if results is None:
                raise BackendError("The notebook left no results.jsonl in the output.")
            meta: dict[str, Any] = {}
            meta_file = next(target.rglob("results.meta.json"), None)
            if meta_file is not None:
                try:
                    loaded = json.loads(meta_file.read_text(encoding="utf-8"))
                    meta = loaded if isinstance(loaded, dict) else {}
                except ValueError:
                    meta = {}
            return Fetched(results.read_bytes(), meta)

    def cleanup(self, ref: dict[str, Any]) -> None:
        # O dataset é privado e pequeno: apagá-lo é arrumação, não obrigação.
        dataset = f"{ref['owner']}/{ref['dataset']}"
        with (
            tempfile.TemporaryDirectory(prefix="ariadne-kaggle-") as tmp,
            contextlib.suppress(BackendError),
        ):
            self._cli(Path(tmp), "datasets", "delete", dataset, "-y")
