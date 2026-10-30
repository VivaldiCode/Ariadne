"""A interface `CourseBackend` (STRUCTURE.md, 6.2): publica um curso canónico numa plataforma.

Como o `WikiBackend` para os artigos: o formato canónico é a fonte de verdade e a plataforma
(hoje o Moodle com CodeRunner) é só a apresentação. Publicar é idempotente: repetir não duplica
nada, e o que não mudou não se toca.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from ariadne.courses.schema import Course


class CourseBackendError(Exception):
    """A plataforma recusou ou falhou ao publicar."""


class CourseBackendUnavailable(CourseBackendError):
    """A plataforma não responde (em baixo, sem configuração, token inválido)."""


@dataclass(frozen=True)
class PublishedActivity:
    """Uma lição publicada: onde ficou na plataforma e o que se lhe fez."""

    ref: str  # «módulo/lição»
    kind: str  # 'page' | 'quiz'
    status: str  # created | updated | unchanged | blocked_by_attempts
    url: str
    activity_id: int  # id da atividade na plataforma (cmid no Moodle)
    instance_id: int  # id do quiz/página (instância)
    question_ids: tuple[int, ...] = ()


@dataclass
class PublishResult:
    course_id: int
    course_url: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    blocked_by_attempts: int = 0
    removed: int = 0
    activities: dict[str, PublishedActivity] = field(default_factory=dict)
    held: dict[str, str] = field(default_factory=dict)  # lição retida (curadoria) -> motivo
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "course_id": self.course_id,
            "course_url": self.course_url,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "blocked_by_attempts": self.blocked_by_attempts,
            "removed": self.removed,
            "held": self.held,
            "warnings": self.warnings,
        }


# Decide se uma lição pode ir para a plataforma: devolve o motivo da retenção, ou `None`.
Gate = Callable[[str], str | None]


class CourseBackend(ABC):
    name: ClassVar[str]

    @abstractmethod
    def check(self) -> dict[str, str]:
        """Confirma que a plataforma responde e o plugin está lá; devolve as versões. Levanta
        `CourseBackendUnavailable` com uma mensagem clara se não."""

    @abstractmethod
    def publish(self, course: Course, *, gate: Gate | None = None) -> PublishResult:
        """Cria ou atualiza o curso. As lições que `gate` retém não se publicam."""

    @abstractmethod
    def close(self) -> None:
        """Liberta as ligações."""
