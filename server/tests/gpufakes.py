"""Um destino de GPU de brincar: o «kernel» responde aos pedidos com o `FakeCurator`."""

import json
from typing import Any

from ariadne.compute.backends import (
    ComputeBackend,
    Fetched,
    Mode,
    PollResult,
    PollState,
    RunSpec,
)
from tests.wikifakes import FakeCurator


class FakeGpuBackend(ComputeBackend):
    """Faz de Kaggle (`id = "kaggle"`, automático). `states` é a sequência que `poll` devolve
    (o último repete-se); `drop` são pedidos que o notebook «não chegou a fazer»."""

    id = "kaggle"
    label = "Kaggle de brincar"
    mode: Mode = "auto"

    def __init__(self, curator: FakeCurator, *, configured: bool = True) -> None:
        self.curator = curator
        self.is_configured = configured
        self.states: list[PollState] = ["running", "complete"]
        self.drop: set[str] = set()  # chaves do pedido (a terceira parte do id) sem resposta
        self.corrupt: set[str] = set()  # chaves com uma resposta que não é JSON
        self.fail_submit: str | None = None
        self.polls = 0
        self.submitted: list[RunSpec] = []
        self.cleaned: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.gpu_seconds = 1800.0

    def credentials(self) -> list[dict[str, Any]]:
        return [{"name": "KAGGLE_API_TOKEN", "set": self.is_configured, "required": True}]

    def default_config(self) -> dict[str, Any]:
        return {"model": "modelo-falso", "weekly_hours": 30.0, "session_hours": 9.0}

    def submit(self, spec: RunSpec) -> dict[str, Any]:
        from ariadne.compute.backends import BackendError

        if self.fail_submit:
            raise BackendError(self.fail_submit)
        self.submitted.append(spec)
        return {
            "kernel": "dono/ariadne-batch",
            "dataset": f"ariadne-batch-{spec.run_id}",
            "owner": "dono",
        }

    def poll(self, ref: dict[str, Any]) -> PollResult:
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return PollResult(state, "")

    def fetch(self, ref: dict[str, Any]) -> Fetched:
        spec = self.submitted[-1]
        out = []
        for line in spec.tasks_jsonl.decode().splitlines():
            request = json.loads(line)
            key = request["id"].split(":", 2)[2]
            if key in self.drop:
                continue
            if key in self.corrupt:
                content = "isto não é JSON"
            else:
                content = self.curator.reply({"messages": request["messages"]})
            out.append(
                json.dumps({"id": request["id"], "content": content, "finish_reason": "stop"})
            )
        return Fetched(
            ("\n".join(out) + "\n").encode(),
            {"gpu_seconds": self.gpu_seconds, "run_id": spec.run_id},
        )

    def cancel(self, ref: dict[str, Any]) -> None:
        self.cancelled.append(ref)

    def cleanup(self, ref: dict[str, Any]) -> None:
        self.cleaned.append(ref)
