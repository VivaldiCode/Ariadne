"""Interface `Translator` (STRUCTURE.md, secção 9): a tradução é um serviço externo, atrás de uma
interface, como o embedder e o Wiki.js. A implementação real é o LibreTranslate."""

from typing import Protocol


class TranslateError(Exception):
    """A tradução falhou (serviço em baixo, língua não suportada, marcadores perdidos…)."""


class TranslateUnavailable(TranslateError):
    """O serviço não respondeu (rede, 5xx): vale a pena não insistir durante um tempo."""


class Translator(Protocol):
    name: str  # identifica o motor na cache (trocá-lo invalida as traduções guardadas)

    def translate(
        self, text: str, source: str, target: str, *, timeout: float | None = None
    ) -> str:
        """Traduz `text` de `source` (ou «auto») para `target`. Levanta `TranslateError`."""
        ...

    def close(self) -> None: ...
