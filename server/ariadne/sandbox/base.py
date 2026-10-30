"""A interface `Sandbox` e os tipos que a acompanham."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum


class SandboxError(Exception):
    """A execução não se fez ou o pedido não faz sentido."""


class SandboxUnavailable(SandboxError):
    """O servidor de execução não responde (em baixo, sem rede, tempo esgotado)."""


class Outcome(IntEnum):
    """Resultado de uma execução (códigos do Jobe)."""

    COMPILATION_ERROR = 11
    RUNTIME_ERROR = 12
    TIME_LIMIT = 13
    SUCCESS = 15
    MEMORY_LIMIT = 17
    ILLEGAL_SYSCALL = 19
    INTERNAL_ERROR = 20
    SERVER_OVERLOAD = 21

    @property
    def label(self) -> str:
        return {
            Outcome.COMPILATION_ERROR: "compilation error",
            Outcome.RUNTIME_ERROR: "runtime error",
            Outcome.TIME_LIMIT: "time limit exceeded",
            Outcome.SUCCESS: "ok",
            Outcome.MEMORY_LIMIT: "memory limit exceeded",
            Outcome.ILLEGAL_SYSCALL: "illegal system call",
            Outcome.INTERNAL_ERROR: "sandbox internal error",
            Outcome.SERVER_OVERLOAD: "sandbox overloaded",
        }[self]


@dataclass(frozen=True)
class Limits:
    """Limites de uma execução. `None` = a omissão do servidor para a linguagem."""

    cpu_seconds: int | None = None
    memory_mb: int | None = None  # memória virtual (o Node precisa de vários GB só para arrancar)
    processes: int | None = None
    disk_mb: int | None = None
    stream_kb: int | None = None  # máximo de stdout/stderr guardado


@dataclass(frozen=True)
class RunResult:
    outcome: Outcome
    stdout: str
    stderr: str
    compile_output: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS


class Sandbox(ABC):
    """Executa código numa linguagem, isolado, com limites. Sem rede para o código executado."""

    @abstractmethod
    def languages(self) -> dict[str, str]:
        """Linguagens disponíveis: id -> versão."""

    @abstractmethod
    def run(
        self,
        language: str,
        source: str,
        *,
        stdin: str = "",
        limits: Limits | None = None,
        filename: str | None = None,
    ) -> RunResult:
        """Corre `source`. Levanta `SandboxUnavailable` se o servidor não responde e
        `SandboxError` se o pedido é inválido (linguagem desconhecida, …)."""

    @abstractmethod
    def close(self) -> None:
        """Liberta as ligações."""
