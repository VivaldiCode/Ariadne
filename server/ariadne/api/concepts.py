"""Conceitos e versões noutras línguas na API REST (ARCHITECTURE.md, secções 8 e 11.2)."""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ariadne.api.deps import AdminDep, PrincipalDep, ServicesDep, SessionDep
from ariadne.calls import tracked_call
from ariadne.concepts import origin
from ariadne.concepts.origin import ConceptInfo
from ariadne.concepts.translations import TranslationsResult
from ariadne.concepts.translations import translations as run_translations

router = APIRouter(tags=["languages"])


@router.get("/translations")
def get_translations(
    services: ServicesDep,
    session: SessionDep,
    principal: PrincipalDep,
    document_id: Annotated[str, Query(description="The document_id (it may contain '/').")],
) -> TranslationsResult:
    """The versions of the same concept in other languages, with the original-language mark (same
    as the MCP `translations` tool). The id goes in a parameter, not in `/documents/{id}/…`: an
    id ending in '/translations' would be ambiguous."""
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="translations",
        arguments={"document_id": document_id},
    ) as call:
        result = run_translations(services, session, document_id)
        call.result = result.call_summary()
    return result


@router.get("/concepts/{ref}")
def get_concept(session: SessionDep, principal: PrincipalDep, ref: str) -> ConceptInfo:
    """A concept by QID (`Q1187566`) or by id: label, original language, document count."""
    return origin.info(session, origin.find_concept(session, ref))


class OriginBody(BaseModel):
    origin_lang: str | None  # ISO 639 (ex.: 'pt'); null apaga


@router.post("/concepts/{ref}")
def set_concept_origin(
    session: SessionDep, principal: AdminDep, ref: str, body: OriginBody
) -> ConceptInfo:
    """Set (or clear, with `null`) a concept's original language: the version in that language
    becomes the "original source" in `translations`, in historical search and in Wiki.js.
    `user` principals only (curation)."""
    arguments = {"concept": ref, "origin_lang": body.origin_lang}
    with tracked_call(
        session,
        principal_id=principal.id,
        session_id=None,
        tool="concept_origin",
        arguments=arguments,
    ) as call:
        result = origin.set_origin(session, ref, body.origin_lang)
        session.commit()
        call.result = {"concept_id": result.id, "origin_lang": result.origin_lang}
    return result
