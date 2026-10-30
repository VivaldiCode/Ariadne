"""Destinos de computação (`ComputeBackend`).

Um destino é um sítio onde o trabalho LLM em lote pode correr:

- `local`: o curador, que é o **Mac Studio** (llama-server com Metal, `CURATOR_URL`), pelos
  workers; não leva lote. O id mantém-se `local` (o painel e a base de dados guardam-no);
- `kaggle`: GPU gratuita da Google, automático (`kaggle.py`: dataset + notebook pela API do Kaggle);
- `colab`: GPU gratuita da Google, manual (o Colab não deixa arrancar runtimes sem API aprovada:
  o Ariadne gera o ficheiro, tu corres o notebook e devolves o resultado);
- `paid`: espaço reservado para um serviço pago (uma API de GPU ou de LLM). Não implementado.

Para acrescentar um destino automático basta uma subclasse de `ComputeBackend` com `submit`,
`poll`, `fetch` (e, se existir, `cancel`/`cleanup`) e o registo em `build_backends`. As credenciais
vêm de variáveis de ambiente (`Settings`); `credentials()` diz quais estão definidas, nunca o valor.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ariadne.config import Settings
from ariadne.models_policy import DEFAULT_BATCH_MODEL

Mode = Literal["local", "auto", "manual", "reserved"]
PollState = Literal["queued", "running", "complete", "failed"]


class BackendError(Exception):
    """O destino recusou ou falhou: a corrida falha e as tarefas voltam à fila."""


@dataclass(frozen=True)
class RunSpec:
    run_id: int
    tasks_jsonl: bytes
    manifest: dict[str, Any]  # modelo, prazo, … (vai com o JSONL para o notebook)
    deadline_seconds: int
    config: dict[str, Any]


@dataclass(frozen=True)
class PollResult:
    state: PollState
    message: str = ""


@dataclass(frozen=True)
class Fetched:
    results_jsonl: bytes
    meta: dict[str, Any] = field(default_factory=dict)  # `results.meta.json` do notebook


class ComputeBackend(ABC):
    id: str
    label: str
    mode: Mode
    description: str = ""
    implemented: bool = True

    def credentials(self) -> list[dict[str, Any]]:
        """As variáveis de ambiente do destino e se estão definidas (nunca o valor)."""
        return []

    def configured(self) -> bool:
        return all(c["set"] for c in self.credentials() if c.get("required", True))

    def missing(self) -> list[str]:
        return [c["name"] for c in self.credentials() if c.get("required", True) and not c["set"]]

    def default_config(self) -> dict[str, Any]:
        """Configuração editável no painel (modelo, horas semanais, …)."""
        return {}

    @abstractmethod
    def submit(self, spec: RunSpec) -> dict[str, Any]:
        """Envia o lote e devolve a referência externa (guardada em `batch_run.external_ref`)."""

    @abstractmethod
    def poll(self, ref: dict[str, Any]) -> PollResult: ...

    @abstractmethod
    def fetch(self, ref: dict[str, Any]) -> Fetched: ...

    def cancel(self, ref: dict[str, Any]) -> None:  # noqa: B027
        """Melhor esforço: alguns destinos não deixam cancelar por API."""

    def cleanup(self, ref: dict[str, Any]) -> None:  # noqa: B027
        """Melhor esforço: apaga o que o lote deixou no destino (datasets temporários)."""


def flag(name: str, value: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "set": bool(value), "required": required}


class LocalBackend(ComputeBackend):
    id = "local"
    label = "Mac (Metal)"
    mode: Mode = "local"
    description = (
        "The local curator (llama-server at CURATOR_URL: the Mac in the ambiente, the `curator` "
        "service in the standalone setup), called by the server workers. No batch: it consumes "
        "the queue task by task, respecting play/pause and the window. While it is off, the "
        "jobs wait in the queue without being lost."
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.label = settings.curator_label or self.label

    def credentials(self) -> list[dict[str, Any]]:
        return [
            flag("CURATOR_URL", self.settings.curator_url),
            flag("CURATOR_MODEL", self.settings.curator_model, required=False),
            flag("CURATOR_API_KEY", self.settings.curator_api_key, required=False),
        ]

    def submit(self, spec: RunSpec) -> dict[str, Any]:
        raise BackendError("The local target takes no batches: the workers consume the queue.")

    def poll(self, ref: dict[str, Any]) -> PollResult:
        raise BackendError("The local target takes no batches.")

    def fetch(self, ref: dict[str, Any]) -> Fetched:
        raise BackendError("The local target takes no batches.")


class ColabManualBackend(ComputeBackend):
    id = "colab"
    label = "Google Colab (manual)"
    mode: Mode = "manual"
    description = (
        "Free Colab GPU. Colab has no API to start runtimes (the API exists in beta, by invitation "
        "only), so: download the package, open the notebook in Colab, run it and upload the "
        "results file here."
    )

    def default_config(self) -> dict[str, Any]:
        return {"model": DEFAULT_BATCH_MODEL}

    def submit(self, spec: RunSpec) -> dict[str, Any]:
        return {"manual": True}

    def poll(self, ref: dict[str, Any]) -> PollResult:
        return PollResult("queued", "waiting for the results file")

    def fetch(self, ref: dict[str, Any]) -> Fetched:
        raise BackendError("Colab is manual: upload the results through the panel.")


class PaidServiceBackend(ComputeBackend):
    """Reservado para um serviço pago. A interface é a de cima: implementar `submit` (enviar o
    JSONL a uma API de GPU ou de LLM), `poll` e `fetch`, e registar em `build_backends`."""

    id = "paid"
    label = "Paid service"
    mode: Mode = "reserved"
    implemented = False
    description = "Not implemented yet: a slot for a paid GPU or LLM API."

    def submit(self, spec: RunSpec) -> dict[str, Any]:
        raise BackendError("The paid service is not implemented yet.")

    def poll(self, ref: dict[str, Any]) -> PollResult:
        raise BackendError("The paid service is not implemented yet.")

    def fetch(self, ref: dict[str, Any]) -> Fetched:
        raise BackendError("The paid service is not implemented yet.")


def build_backends(settings: Settings) -> dict[str, ComputeBackend]:
    from ariadne.compute.kaggle import KaggleBackend

    backends: list[ComputeBackend] = [
        LocalBackend(settings),
        KaggleBackend(settings),
        ColabManualBackend(),
        PaidServiceBackend(),
    ]
    return {b.id: b for b in backends}
