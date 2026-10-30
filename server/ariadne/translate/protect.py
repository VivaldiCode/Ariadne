"""Proteger o que não se traduz (STRUCTURE.md, secção 9): antes de traduzir, o texto troca por
marcadores o código (blocos e inline), as citações `[doc:…]`, os URL, os nomes de funções e
identificadores e uma lista configurável de termos técnicos; depois da tradução repõem-se.

Os marcadores são `QXZ0000`, `QXZ0001`, …: letras e dígitos sem pontuação nem espaços e sem sufixo,
que o motor copia tal e qual (medido com o LibreTranslate/Argos PT⇄EN: 16 de 16 frases com 2–3
marcadores; um marcador com sufixo, como `ZQ0QZ`, deixou o motor enfiar palavras lá dentro). Se
algum se perder ou se repetir, a tradução é recusada (`TranslateError`) em vez de devolver código
estragado."""

import re
import secrets
import string
from collections.abc import Sequence

from ariadne.translate.base import TranslateError

PROTECT_VERSION = "2"  # entra na chave da cache: mudar o que se protege invalida as traduções
PREFIX = "QXZ"

DEFAULT_TERMS = (
    "Python,Docker,docker-compose,PostgreSQL,Postgres,Kubernetes,JSON,YAML,API,REST,SQL,HTTP,HTTPS,"
    "Git,GitHub,Linux,macOS,Windows,pgvector,Wiki.js,Kiwix,LibreTranslate,MCP,LLM,CPU,GPU,RAM,"
    "JavaScript,TypeScript,Node.js,React,FastAPI,SQLAlchemy,Markdown,HTML,CSS,URL,UTF-8,ONNX,"
    "ariadne,Ariadne"
)

FENCED = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_CITATION = re.compile(r"\[doc:[^\]\n]*\]")
# Parênteses só se vierem em par (`…_(1974)`): o `)` que fecha uma frase não faz parte do URL.
_URL = re.compile(
    r"https?://(?:[^\s<>\"'()\[\]]|\([^\s()]*\))*(?:[^\s<>\"'()\[\].,;:!?]|\([^\s()]*\))"
)
_CALL = re.compile(r"\b[A-Za-z_][\w.]*\([^()\n]*\)")
_SNAKE = re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b")
_CAMEL = re.compile(r"\b(?:[A-Za-z]*[a-z][A-Z]|[A-Z]{2,}[a-z])[A-Za-z0-9]*\b")
_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_PATH = re.compile(r"(?<![\w/])(?:\./|\.\./|~/|/)[\w.-]+(?:/[\w.-]+)*")
_FILE = re.compile(r"\b[\w-]+(?:/[\w-]+)+\.\w{1,5}\b")


def token_re(prefix: str = PREFIX) -> re.Pattern[str]:
    return re.compile(re.escape(prefix) + r"\s?(\d{4})", re.IGNORECASE)


def choose_prefix(text: str) -> str:
    """O prefixo dos marcadores deste pedido: `QXZ`, a não ser que o próprio texto já tenha algo
    que pareça um marcador (`QXZ0000` num artigo); então um prefixo aleatório (um *nonce*) que não
    aparece no texto, para o literal não se confundir com um marcador."""
    prefix = PREFIX
    while token_re(prefix).search(text):
        prefix = "".join(secrets.choice(string.ascii_uppercase) for _ in range(3))
    return prefix


def parse_terms(raw: str) -> list[str]:
    """«Python, Docker,…» -> lista sem vazios nem repetidos, do termo maior para o menor."""
    terms = dict.fromkeys(t.strip() for t in raw.split(",") if t.strip())
    return sorted(terms, key=lambda t: (-len(t), t))


def _spans(text: str, terms: Sequence[str]) -> list[tuple[int, int]]:
    patterns = [
        FENCED, _INLINE_CODE, _CITATION, _URL, _CALL, _FILE, _PATH, _DOTTED, _SNAKE, _CAMEL,
    ]  # fmt: skip
    if terms:
        alternatives = "|".join(re.escape(t) for t in terms)
        patterns.append(re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])"))
    found: list[tuple[int, int]] = []
    for pattern in patterns:
        found.extend((m.start(), m.end()) for m in pattern.finditer(text))
    found.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    chosen: list[tuple[int, int]] = []
    last_end = 0
    for start, end in found:  # sem sobreposições: ganha o que começa primeiro, depois o maior
        if start >= last_end:
            chosen.append((start, end))
            last_end = end
    return chosen


def protect(text: str, terms: Sequence[str] = (), prefix: str = PREFIX) -> tuple[str, list[str]]:
    """O texto com os marcadores no lugar do que não se traduz, e a lista do que lá estava."""
    saved: list[str] = []
    parts: list[str] = []
    cursor = 0
    for start, end in _spans(text, terms):
        parts.append(text[cursor:start])
        parts.append(f"{prefix}{len(saved):04d}")
        saved.append(text[start:end])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), saved


def restore(translated: str, saved: Sequence[str], prefix: str = PREFIX) -> str:
    """Repõe o que `protect` guardou. Cada marcador tem de aparecer exatamente uma vez."""
    seen: list[int] = []

    def put_back(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(saved):
            raise TranslateError("The translation invented a code marker")
        seen.append(index)
        return saved[index]

    restored = token_re(prefix).sub(put_back, translated)
    if sorted(seen) != list(range(len(saved))):
        raise TranslateError("The translation lost or repeated code markers")
    return restored


def has_words(masked: str, prefix: str = PREFIX) -> bool:
    """Sobra algo para traduzir além dos marcadores?"""
    return any(c.isalpha() for c in token_re(prefix).sub("", masked))
