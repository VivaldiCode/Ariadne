"""Tradutor de brincar para os testes: troca palavras por um dicionário, sem rede."""

import re
from collections.abc import Mapping

from ariadne.translate.base import TranslateUnavailable

PT_EN = {
    "revolução": "revolution", "dos": "of the", "cravos": "carnations", "quando": "when",
    "foi": "was", "a": "the", "capital": "capital", "de": "of", "portugal": "portugal",
    "golpe": "coup", "militar": "military", "democracia": "democracy", "guerra": "war",
    "como": "how", "instalar": "install", "o": "the", "código": "code", "função": "function",
    "exemplo": "example", "veja": "see", "texto": "text", "isto": "this", "é": "is",
    "um": "a", "uma": "a", "teste": "test", "usa": "uses", "para": "for",
}  # fmt: skip
EN_PT = {
    "carnation": "cravo", "revolution": "revolução", "when": "quando", "was": "foi",
    "the": "a", "capital": "capital", "of": "de", "portugal": "portugal", "coup": "golpe",
    "military": "militar", "how": "como", "to": "para", "install": "instalar",
    "code": "código", "function": "função", "example": "exemplo", "see": "veja",
    "text": "texto", "this": "isto", "is": "é", "a": "um", "test": "teste", "uses": "usa",
    "for": "para", "war": "guerra", "democracy": "democracia",
}  # fmt: skip
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


class FakeTranslator:
    """Traduz palavra a palavra (PT↔EN) e deixa o resto (números, símbolos, marcadores) como está.
    `down` faz tudo falhar; `lose_markers` apaga os marcadores de código, como um motor que os
    comesse; `calls` guarda os pedidos."""

    name = "fake"

    def __init__(self, dictionaries: Mapping[tuple[str, str], Mapping[str, str]] | None = None):
        self.dictionaries = dictionaries or {("pt", "en"): PT_EN, ("en", "pt"): EN_PT}
        self.calls: list[tuple[str, str, str]] = []
        self.down = False
        self.lose_markers = False
        self.closed = False

    def translate(
        self, text: str, source: str, target: str, *, timeout: float | None = None
    ) -> str:
        self.calls.append((text, source, target))
        if self.down:
            raise TranslateUnavailable("tradutor de brincar em baixo")
        words = self.dictionaries.get((source, target), {})

        def swap(match: re.Match[str]) -> str:
            word = match.group(0)
            new = words.get(word.casefold(), word)
            return new.capitalize() if word[:1].isupper() and new != word else new

        out = _WORD.sub(swap, text)
        if self.lose_markers:
            out = re.sub(r"[A-Z]{3}\d{4}", "", out)  # qualquer prefixo (muda com o nonce do pedido)
        return out

    def close(self) -> None:
        self.closed = True
