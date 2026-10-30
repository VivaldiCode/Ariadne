"""Texto vindo de fora (agentes, URLs) que o Postgres aceita: sem NUL nem surrogates soltos."""

import re
import unicodedata
from typing import Any

_SPACES = re.compile(r"\s+")
_TRAILING_PUNCTUATION = " .,;:!?…"


def clean_text(text: str) -> str:
    """Tira o NUL (o Postgres não o guarda em text/jsonb) e troca surrogates soltos por '?'."""
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def clean_json(value: Any) -> Any:
    """`clean_text` em todas as strings (e chaves) de uma estrutura JSON."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, dict):
        return {clean_text(str(key)): clean_json(item) for key, item in value.items()}
    return value


def title_key(title: str) -> str:
    """Chave de comparação de títulos entre fontes (`title_match`, secção 7.2): sem maiúsculas
    (casefold), '_' e espaços repetidos viram um espaço, sem pontuação no fim. Os acentos contam:
    'Revolução' e 'Revolucao' são títulos diferentes."""
    text = unicodedata.normalize("NFKC", clean_text(title)).replace("_", " ")
    return _SPACES.sub(" ", text).strip().casefold().rstrip(_TRAILING_PUNCTUATION).strip()
