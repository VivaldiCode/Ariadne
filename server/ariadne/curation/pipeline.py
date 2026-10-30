"""Estado da curadoria na base de dados (tabela `curation`) e as decisões da pipeline.

    texto limpo ──▶ flags / saúde / amostragem ──▶ nenhuma?  ──▶ published (sem curador)
                                                  alguma?   ──▶ pending_curator ──▶ curador ──▶
                    fidelidade ok ──▶ published     |  falhou ou o curador rejeitou ──▶ needs_review
                    revisão humana: aprovar ──▶ published, rejeitar ──▶ rejected

Só `published` se publica no Wiki.js. Os links internos guardam-se como `ariadne:<caminho>` e só
são resolvidos ao publicar (para a página do Wiki.js se o destino lá está, senão para o kiwix):
assim a impressão digital do texto não muda quando outro artigo é importado.
"""

import hashlib
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ariadne.curation.flags import Flag, flags_json
from ariadne.db.models import Curation
from ariadne.documents import LibraryError

PUBLISHED, PENDING, REVIEW, REJECTED = "published", "pending_curator", "needs_review", "rejected"
DECISIONS = (PUBLISHED, PENDING, REVIEW, REJECTED)
LINK_SCHEME = "ariadne:"
_LINK_TEXT = r"((?:[^\]\\]|\\.)*)"  # o texto de um link, com \] escapados
_TOKEN = re.compile(rf"\[{_LINK_TEXT}\]\(ariadne:([^)\s]*)(?:\s+\"[^\"]*\")?\)")


class CurationConflict(LibraryError):
    """O texto mudou desde que o revisor o viu: tem de voltar a ver."""


def link_token(path: str) -> str:
    return f"{LINK_SCHEME}{quote(path, safe='')}"


def resolve_tokens(markdown: str, lookup: Callable[[str], str | None]) -> str:
    """Troca os links `ariadne:<caminho>` pelo URL que `lookup` der; sem destino, fica o texto."""

    def replace(match: re.Match[str]) -> str:
        url = lookup(unquote(match.group(2)))
        return f"[{match.group(1)}]({url})" if url else match.group(1)

    return _TOKEN.sub(replace, markdown)


def token_paths(markdown: str) -> set[str]:
    return {unquote(m.group(2)) for m in _TOKEN.finditer(markdown)}


def fingerprint(markdown: str) -> str:
    """Impressão digital do texto limpo (STRUCTURE.md, 5: «Atualizações»)."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def register(
    session: Session,
    *,
    document_id: str,
    version: str,
    original_md: str,
    flags: list[Flag],
    sampled: bool,
) -> Curation:
    """Regista o texto limpo atual e decide o que lhe acontece. Texto igual ao que já está
    registado não muda nada (só a versão da fonte); texto alterado volta a ser avaliado do zero,
    mantendo o que estava publicado até o novo passar. Bloqueia a linha."""
    digest = fingerprint(original_md)
    row = session.scalars(
        select(Curation).where(Curation.document_id == document_id).with_for_update()
    ).first()
    needs = bool(flags) or sampled
    if row is not None and row.fingerprint == digest:
        row.source_version = version
        if row.archived_at is not None:  # o artigo reapareceu com o mesmo texto: sai do arquivo
            row.archived_at = None
            row.updated_at = func.now()
        return row
    if row is None:
        row = Curation(document_id=document_id, created_at=func.now())
        session.add(row)
    row.source_version = version
    row.fingerprint = digest
    row.original_md = original_md
    row.flags = flags_json(flags)
    row.sampled = sampled
    row.curated_md = None
    row.fidelity = None
    row.curator_model = None
    row.reviewed_by = None
    row.reviewed_at = None
    row.archived_at = None
    row.batch_run_id = None
    row.decision = PENDING if needs else PUBLISHED
    row.reason = _why(flags, sampled) if needs else "no flags: deterministic cleanup"
    row.updated_at = func.now()
    return row


def _why(flags: list[Flag], sampled: bool) -> str:
    codes = sorted({f.code for f in flags})
    reasons = [f"flags: {', '.join(codes)}"] if codes else []
    if sampled:
        reasons.append("sampling")
    return "sent to the curator (" + "; ".join(reasons) + ")"


def finish_curation(
    session: Session,
    document_id: str,
    digest: str,
    *,
    decision: str,
    reason: str,
    model: str | None,
    curated_md: str | None,
    fidelity: dict[str, Any] | None,
    batch_run_id: int | None = None,
) -> bool:
    """Grava o resultado do curador, se o texto não mudou entretanto. Devolve se gravou."""
    updated = session.scalar(
        update(Curation)
        .where(
            Curation.document_id == document_id,
            Curation.fingerprint == digest,
            Curation.decision == PENDING,
        )
        .values(
            decision=decision,
            reason=reason,
            curator_model=model,
            curated_md=curated_md,
            fidelity=fidelity,
            batch_run_id=batch_run_id,
            updated_at=func.now(),
        )
        .returning(Curation.document_id)
    )
    return updated is not None


def review(
    session: Session,
    document_id: str,
    approve: bool,
    reviewer: str,
    note: str | None,
    fingerprint: str,
) -> Curation | None:
    """Decisão humana: aprovar publica (`curated_md` se há, senão o texto limpo); rejeitar retém.
    `None` se o documento não tem curadoria."""
    row = session.scalars(
        select(Curation).where(Curation.document_id == document_id).with_for_update()
    ).first()
    if row is None:
        return None
    if row.fingerprint != fingerprint:
        raise CurationConflict(
            "The text changed since you saw it (different fingerprint): open the item again."
        )
    row.decision = PUBLISHED if approve else REJECTED
    row.reason = ("approved" if approve else "rejected") + " by human review"
    if note:
        row.reason += f": {note[:300]}"
    row.reviewed_by = reviewer
    row.reviewed_at = func.now()
    row.updated_at = func.now()
    return row


_UNSAFE_TAG = re.compile(
    r"</?\s*(?:script|iframe|object|embed|style|link|meta|form|input|svg|base|img|video|audio)"
    r"\b[^>]*>",
    re.IGNORECASE,
)
_ANY_LINK = re.compile(rf"(!?)\[{_LINK_TEXT}\]\(([^)\s]*)[^)]*\)")
_REF_DEFINITION = re.compile(r"^[ ]{0,3}\[[^\]]+\]:[ \t]*\S.*$", re.MULTILINE)
_REF_LINK = re.compile(rf"\[{_LINK_TEXT}\]\[[^\]]*\]")
_BARE_URL = re.compile(r"(?:https?:)?//[^\s<>()\[\]\"']+", re.IGNORECASE)
_CODE_REGION = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
MAX_SANITIZE_PASSES = 5


def link_urls(markdown: str) -> set[str]:
    """Os URLs que um texto já tem (destinos de links e URLs soltos): são os únicos que a
    reescrita do curador pode manter."""
    urls = {m.group(3) for m in _ANY_LINK.finditer(markdown) if not m.group(1)}
    return urls | {m.group(0).rstrip(".,;:") for m in _BARE_URL.finditer(markdown)}


def _escape_angles(markdown: str) -> str:
    """Nenhum `<` fora de código chega ao Wiki.js: não há etiquetas (nem `<img onerror>`)."""
    out: list[str] = []
    last = 0
    for region in _CODE_REGION.finditer(markdown):
        out.append(markdown[last : region.start()].replace("<", "&lt;"))
        out.append(region.group(0))
        last = region.end()
    out.append(markdown[last:].replace("<", "&lt;"))
    return "".join(out)


def _sanitize_once(markdown: str, allowed_paths: set[str], allowed_urls: set[str]) -> str:
    def link(match: re.Match[str]) -> str:
        image, text, url = match.group(1), match.group(2), match.group(3)
        if image:
            return text
        if url.startswith(LINK_SCHEME):
            return match.group(0) if unquote(url[len(LINK_SCHEME) :]) in allowed_paths else text
        return match.group(0) if url in allowed_urls else text

    def bare(match: re.Match[str]) -> str:
        return match.group(0) if match.group(0).rstrip(".,;:") in allowed_urls else ""

    text = _UNSAFE_TAG.sub("", markdown)
    text = _REF_DEFINITION.sub("", text)
    text = _REF_LINK.sub(lambda m: m.group(1), text)
    text = _ANY_LINK.sub(link, text)
    # URLs soltos (o Wiki.js transforma-os em links) fora dos destinos já tratados acima.
    text = _BARE_URL.sub(bare, text)
    return _escape_angles(text)


def sanitize_curated(
    markdown: str, allowed_paths: set[str], allowed_urls: set[str] | None = None
) -> str:
    """A saída do curador é texto de um modelo: repete-se a limpeza até estabilizar. Tira etiquetas
    HTML (e escapa qualquer `<` fora de código), imagens, links de referência, links internos
    novos e todo o URL (em link ou solto, `javascript:`, `data:` e `//host` incluídos) que o
    original não tinha."""
    urls = allowed_urls or set()
    text = markdown
    for _ in range(MAX_SANITIZE_PASSES):
        cleaned = _sanitize_once(text, allowed_paths, urls)
        if cleaned == text:
            break
        text = cleaned
    return text
