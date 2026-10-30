"""Tradução na API REST (STRUCTURE.md, secção 9): a app oferece versões traduzidas de texto."""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ariadne.api.deps import PrincipalDep, ServicesDep, SessionDep
from ariadne.calls import tracked_call
from ariadne.translate.service import TranslateResult


def build_router(max_chars: int) -> APIRouter:
    """O router de `/translate`; o tamanho máximo do texto (`TRANSLATE_MAX_CHARS`) entra no
    próprio esquema do pedido, por isso um corpo enorme é recusado (422) antes de se processar."""
    router = APIRouter(tags=["translation"])

    class TranslateBody(BaseModel):
        text: str = Field(max_length=max_chars, description="The text to translate.")
        target: str = Field(max_length=16, description="Target language (ISO 639-1, e.g. pt, en).")
        source: str | None = Field(
            None, max_length=16, description="Source language; if omitted, it is detected."
        )
        document_id: str | None = Field(
            None,
            description="The document the text comes from: if it is private, it goes only to a "
            "translator on the local list (PRIVATE_ALLOWED_HOSTS).",
        )

    @router.post("/translate")
    def translate(
        services: ServicesDep, session: SessionDep, principal: PrincipalDep, body: TranslateBody
    ) -> TranslateResult:
        """Translate a text with the local LibreTranslate (same as the MCP `translate` tool).
        Code, `[doc:…]` citations, URLs and technical terms are not translated. If the service
        is down or off, `translated` is false and `text` is the original, with a `warning`. With
        too many translations in progress it answers 429."""
        arguments = {"target": body.target, "source": body.source, "chars": len(body.text)}
        principal_id = principal.id  # lido antes do commit, que expira o objeto
        session.commit()  # liberta a ligação à base de dados antes do trabalho lento
        with tracked_call(
            session,
            principal_id=principal_id,
            session_id=None,
            tool="translate",
            arguments=arguments,
        ) as call:
            result = services.get_translation().translate(
                body.text, body.target, body.source, document_id=body.document_id
            )
            call.result = result.call_summary()
        return result

    return router
