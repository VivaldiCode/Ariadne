"""Registo dos tipos de tarefa que podem correr em lote numa GPU externa.

Um tipo de tarefa (`TaskType`) é uma classe com o `kind` do job da fila que trata e duas
operações:

- `prepare(...)` transforma um job pendente em pedidos ao LLM (mensagens, limite de tokens) e num
  `meta` para a importação (a impressão digital do texto, o documento, …);
- `apply(...)` recebe as respostas do LLM e valida-as, com as mesmas regras do curador local, e
  grava o resultado. Devolve `Applied` com o destino do job.

O executor (`ariadne.compute.runs`) é genérico: exporta o JSONL, importa o resultado, conta, e
devolve à fila o que a GPU externa não consumiu. Nenhum tipo precisa de saber de Kaggle ou Colab.

Para acrescentar um tipo (ex.: `format_rescue`, `domain_classify`):

1. escrever uma subclasse de `TaskType` com `kind`, `prompt_version`, `prepare` e `apply`;
2. chamar `register(MeuTipo())` ao importar o módulo (e importá-lo em `ariadne.compute.__init__`);
3. o handler local do job (o que o worker já tem para esse `kind`) fica como está: o `Runner`
   passa a respeitar sozinho o painel (pausa, janela, destino) para todos os tipos registados.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ariadne.db.models import Job
from ariadne.services import Services


@dataclass(frozen=True)
class BatchRequest:
    """Um pedido ao LLM: uma linha do JSONL. `key` é único dentro da tarefa."""

    key: str
    messages: list[dict[str, str]]
    max_tokens: int = 4096
    temperature: float = 0.0


@dataclass(frozen=True)
class BatchReply:
    """A resposta a um pedido: o texto do modelo, ou o erro que o notebook registou."""

    content: str | None
    error: str | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class Prepared:
    """O que um job vai pedir ao LLM e o que a importação precisa de saber para o aplicar."""

    requests: list[BatchRequest]
    meta: dict[str, Any] = field(default_factory=dict)


# Destinos possíveis de uma tarefa depois de aplicada.
APPLIED = "applied"  # resultado aceite e gravado (o job fica concluído)
REVIEW = "review"  # gravado, mas fica para revisão humana (o job fica concluído)
INVALID = "invalid"  # a resposta não serve (esquema, fidelidade): o curador local tenta
STALE = "stale"  # o texto mudou entretanto: já não faz falta (o job fica concluído)


@dataclass(frozen=True)
class Applied:
    outcome: str  # APPLIED | REVIEW | INVALID | STALE
    detail: str = ""


class TaskType(ABC):
    kind: str
    prompt_version: str = "1"

    @abstractmethod
    def prepare(self, services: Services, session: Session, job: Job) -> Prepared | None:
        """Os pedidos deste job, ou `None` se não vai em lote (já decidido, conteúdo privado, …):
        fica na fila e o handler local trata dele (ou conclui-o sem trabalho)."""

    @abstractmethod
    def apply(
        self,
        services: Services,
        session: Session,
        job: Job,
        meta: dict[str, Any],
        replies: dict[str, BatchReply],
        *,
        run_id: int,
        model: str | None,
    ) -> Applied:
        """Valida e grava as respostas (`replies`: chave do pedido -> resposta). Respostas em falta
        não estão no dicionário: o tipo decide se isso invalida a tarefa."""


_REGISTRY: dict[str, TaskType] = {}


def register(task_type: TaskType) -> TaskType:
    _REGISTRY[task_type.kind] = task_type
    return task_type


def get(kind: str) -> TaskType | None:
    return _REGISTRY.get(kind)


def kinds() -> list[str]:
    """Os tipos registados, por ordem de nome."""
    return sorted(_REGISTRY)


def request_id(run_id: int, job_id: int, key: str) -> str:
    """O id de uma linha do JSONL: liga o resultado à corrida, ao job e ao pedido."""
    return f"r{run_id}:j{job_id}:{key}"


def parse_request_id(value: str) -> tuple[int, int, str] | None:
    """Inverso de `request_id`; `None` se o id não tem esta forma."""
    try:
        run, job, key = value.split(":", 2)
        if run[0] != "r" or job[0] != "j":
            return None
        return int(run[1:]), int(job[1:]), key
    except (ValueError, IndexError):
        return None
