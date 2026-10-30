"""Curadoria das lições (STRUCTURE.md, secção 5): a pipeline da Fase 6 aplicada aos blocos de texto.

Cada lição passa pela triagem por flags (injeção, texto escondido, blocos codificados, comandos
perigosos, saúde) sobre o texto dos blocos e sobre o código; o resultado fica na tabela `curation`
como nos artigos (`published`, `pending_curator`, `needs_review`, `rejected`). Só uma lição
`published` vai para o Moodle: as outras ficam retidas, com o motivo, até o curador ou a revisão
humana as aprovarem (ou até o texto mudar e a triagem refazer-se).

O curador (LLM) lê o texto dos blocos e a decisão dele e a da fidelidade comandam a retenção; a
reescrita fica guardada em `curation.curated_md`, mas **não** é escrita de volta nos blocos do
canónico (a divisão do texto reescrito pelos blocos não é fiável): o canónico continua a ser o
texto da fonte.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ariadne import queue
from ariadne.config import Settings
from ariadne.courses.schema import (
    CodeExampleBlock,
    Course,
    ExerciseBlock,
    Lesson,
    QuizBlock,
    SolutionBlock,
    TextBlock,
    VideoBlock,
    WorkedExampleBlock,
)
from ariadne.curation import flags as flagging
from ariadne.curation import pipeline
from ariadne.curation.curator import CURATOR_KIND
from ariadne.db.models import Curation
from ariadne.licenses import curator_gate

CURATE_PRIORITY = 0.4


def lesson_text(lesson: Lesson) -> str:
    """O texto dos blocos de uma lição (o que o curador lê e a triagem de prosa vê)."""
    parts: list[str] = []
    for block in lesson.blocks:
        if isinstance(block, TextBlock):
            parts.append(block.markdown)
        elif isinstance(block, WorkedExampleBlock):
            parts += [block.statement, block.explanation]
        elif isinstance(block, ExerciseBlock):
            parts.append(block.statement)
        elif isinstance(block, QuizBlock):
            for question in block.questions:
                parts += [
                    question.prompt,
                    *(c.text for c in question.choices),
                    question.explanation,
                ]
        elif isinstance(block, VideoBlock):
            parts.append(block.transcript)
        elif isinstance(block, SolutionBlock):
            parts.append(block.explanation)
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def lesson_code(lesson: Lesson) -> list[str]:
    """O código da lição: exemplos, código inicial, arnês, testes e soluções."""
    code: list[str] = []
    for block in lesson.blocks:
        if isinstance(block, CodeExampleBlock | SolutionBlock | WorkedExampleBlock):
            code.append(block.code)
        elif isinstance(block, ExerciseBlock):
            code += [block.starter_code, block.harness_before, block.harness_after]
            code += [test.code for test in block.tests]
    return [c for c in code if c.strip()]


def evaluate_lesson(
    session: Session, settings: Settings, *, document_id: str, source_id: str, version: str,
    lesson: Lesson,
) -> Curation:  # fmt: skip
    """Regista a triagem de uma lição em `curation` (sem commit)."""
    text = lesson_text(lesson)
    flags = flagging.detect(text, code=lesson_code(lesson), settings=settings, source_id=source_id)
    row = pipeline.register(
        session,
        document_id=document_id,
        version=version,
        original_md=text,
        flags=flags,
        sampled=flagging.sampled(document_id, settings.curation_sample_rate),
    )
    session.flush()
    if (
        row.decision == pipeline.PENDING
        and settings.curator_url
        and curator_gate(session, settings, row)
    ):
        queue.enqueue(
            session,
            CURATOR_KIND,
            {"document_id": document_id},
            priority=CURATE_PRIORITY,
            dedupe_key=f"{CURATOR_KIND}:{document_id}",
        )
    return row


@dataclass(frozen=True)
class Verdict:
    publishable: bool
    reason: str | None  # porque está retida


def verdict(decision: str | None, reason: str | None) -> Verdict:
    if decision in (None, pipeline.PUBLISHED):
        return Verdict(True, None)
    why = {
        pipeline.PENDING: "waiting for the curator",
        pipeline.REVIEW: "waiting for human review",
        pipeline.REJECTED: "rejected in human review",
    }.get(decision, f"curation: {decision}")
    return Verdict(False, f"{why} ({reason})" if reason else why)


def held_for_version(session: Session, settings: Settings, course: Course) -> dict[str, str]:
    """As lições de uma versão que não vão para a plataforma («módulo/lição» -> motivo), sem tocar
    na base. A curadoria vale para **qualquer** versão que se publique, não só a atual: o texto de
    uma versão antiga pode ter flags que a atual já não tem. Se a decisão guardada é sobre este
    mesmo texto (a mesma impressão digital), vale; senão faz-se a triagem na hora e só passa o
    que não dispara flags nem cai na amostragem."""
    held: dict[str, str] = {}
    source_id = f"course:{course.id}"
    for module, lesson in course.walk():
        ref = f"{module.id}/{lesson.id}"
        document_id = f"{source_id}/{ref}"
        text = lesson_text(lesson)
        row = session.get(Curation, document_id)
        if row is not None and row.fingerprint == pipeline.fingerprint(text):
            result = verdict(row.decision, row.reason)
            if not result.publishable and result.reason:
                held[ref] = result.reason
            continue
        flags = flagging.detect(
            text, code=lesson_code(lesson), settings=settings, source_id=source_id
        )
        sampled = flagging.sampled(document_id, settings.curation_sample_rate)
        if flags or sampled:
            codes = ", ".join(sorted({f.code for f in flags})) or "sampling"
            held[ref] = f"waiting for the curator (this version's text was not approved; {codes})"
    return held
