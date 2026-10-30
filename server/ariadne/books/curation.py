"""Curadoria dos capítulos (STRUCTURE.md, secção 5): a pipeline da Fase 6 aplicada ao texto de
cada capítulo.

Cada capítulo passa pela triagem por flags (injeção, texto escondido, blocos codificados, comandos
perigosos em código, saúde) e o resultado fica na tabela `curation` como nos artigos e nas lições
(`published`, `pending_curator`, `needs_review`, `rejected`). Num livro não há uma plataforma que
publique capítulo a capítulo (o Kavita mostra o ficheiro inteiro), por isso a decisão serve de
**sinal**: um capítulo com flags continua pesquisável (a pesquisa local vê tudo), mas a pesquisa e
`read` assinalam-no, e um capítulo `rejected` (revisão humana) deixa de aparecer nos resultados.
O curador (LLM) corre como nos artigos; a reescrita fica em `curation.curated_md`.
"""

from dataclasses import dataclass

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from ariadne import queue
from ariadne.books.extract import ExtractedChapter
from ariadne.config import Settings
from ariadne.courses.curation import verdict
from ariadne.curation import flags as flagging
from ariadne.curation import pipeline
from ariadne.curation.curator import CURATOR_KIND
from ariadne.db.models import Curation
from ariadne.licenses import curator_gate

CURATE_PRIORITY = 0.3  # os capítulos vêm depois dos artigos e das lições

__all__ = ["ChapterVerdict", "evaluate_chapter"]


@dataclass(frozen=True)
class ChapterVerdict:
    decision: str
    held_reason: str | None  # porque está assinalado (None = publicado, sem flags)


def evaluate_chapter(
    session: Session,
    settings: Settings,
    *,
    document_id: str,
    source_id: str,
    version: str,
    chapter: ExtractedChapter,
    text: str,
) -> tuple[Curation, ChapterVerdict]:
    """Regista a triagem de um capítulo em `curation` (sem commit). `text` é o texto que `read`
    devolve; o código e o texto escondido vêm do HTML do capítulo."""
    code = flagging.code_texts(BeautifulSoup(chapter.html, "html.parser"))
    flags = flagging.detect(
        text, code=code, hidden=chapter.hidden, settings=settings, source_id=source_id
    )
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
    result = verdict(row.decision, row.reason)
    return row, ChapterVerdict(row.decision, None if result.publishable else result.reason)
