"""Sandbox de código (STRUCTURE.md, secção 12): a única maneira de o Ariadne executar código.

Os agentes nunca executam nada: pesquisam e leem. Quem precisa de correr código (o CodeRunner do
Moodle, a verificação das soluções dos cursos) fala com um `Sandbox`, hoje o Jobe.
"""

from ariadne.sandbox.base import (
    Limits,
    Outcome,
    RunResult,
    Sandbox,
    SandboxError,
    SandboxUnavailable,
)
from ariadne.sandbox.jobe import JobeSandbox

__all__ = [
    "JobeSandbox",
    "Limits",
    "Outcome",
    "RunResult",
    "Sandbox",
    "SandboxError",
    "SandboxUnavailable",
]
