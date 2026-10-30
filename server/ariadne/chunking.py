"""Divide o texto de um documento em chunks de ~`words` palavras (ARCHITECTURE.md, secção 10).

Respeita parágrafos: um chunk junta parágrafos inteiros até chegar ao tamanho; um parágrafo maior
que isso parte-se por frases (e, em último caso, por palavras). Um título de secção nunca fica
sozinho no fim de um chunk. O título do artigo abre cada chunk («Título — …»), para o vetor saber
de que trata mesmo quando o excerto não o diz.
"""

import re

DEFAULT_WORDS = 180  # ≈ 256 tokens
MIN_TAIL_WORDS = 20  # uma cauda mais pequena que isto cola-se ao chunk anterior
TITLE_SEPARATOR = " — "

_PARAGRAPHS = re.compile(r"\n\s*\n")
# Fim de frase: pontuação latina seguida de espaço, ou pontuação CJK (que não leva espaço).
_SENTENCES = re.compile(r"(?<=[.!?…])\s+(?=\S)|(?<=[。！？])")
MAX_RUN_CHARS = 10  # uma palavra normal cabe numa unidade; texto colado conta por pedaços

# Uma «palavra» para o tamanho do chunk: cada carácter CJK (chinês, japonês, coreano) conta como
# uma, porque não há espaços e o modelo gasta ~1 token por carácter; o resto são sequências sem
# espaços, e as muito compridas (URLs, texto colado) contam por pedaços de MAX_RUN_CHARS.
_CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
_UNIT = re.compile(rf"[{_CJK}]|[^\s{_CJK}]{{1,{MAX_RUN_CHARS}}}")


def chunk_document(title: str, text: str, words: int = DEFAULT_WORDS) -> list[str]:
    """Os chunks do documento, já com o título à frente. Texto vazio dá lista vazia."""
    prefix = f"{title.strip()}{TITLE_SEPARATOR}" if title.strip() else ""
    return [prefix + body for body in split_text(text, words)]


def strip_title(chunk: str, title: str) -> str:
    """O inverso do prefixo: o texto do chunk sem «Título — » (para mostrar como excerto)."""
    prefix = f"{title.strip()}{TITLE_SEPARATOR}"
    return chunk[len(prefix) :] if title.strip() and chunk.startswith(prefix) else chunk


def split_text(text: str, words: int = DEFAULT_WORDS) -> list[str]:
    words = max(1, words)
    pieces: list[str] = []
    for paragraph in _PARAGRAPHS.split(text):
        paragraph = paragraph.strip()
        if paragraph:
            pieces.extend(_split_paragraph(paragraph, words))

    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for piece in pieces:
        n = _count(piece)
        if current and size + n > words:
            carried = [current.pop()] if _is_heading(current[-1]) and len(current) > 1 else []
            chunks.append(current)
            current = carried
            size = sum(_count(p) for p in current)
        current.append(piece)
        size += n
    if current:
        chunks.append(current)

    result = ["\n\n".join(parts) for parts in chunks]
    if len(result) > 1 and _count(result[-1]) < min(MIN_TAIL_WORDS, words // 2):
        tail = result.pop()
        result[-1] += "\n\n" + tail
    return result


def _count(text: str) -> int:
    return len(_UNIT.findall(text))


def _is_heading(piece: str) -> bool:
    return piece.startswith("#") and "\n" not in piece


def _split_paragraph(paragraph: str, words: int) -> list[str]:
    """Um parágrafo que cabe fica inteiro; senão parte-se por frases, depois por palavras."""
    if _count(paragraph) <= words:
        return [paragraph]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for sentence in _SENTENCES.split(paragraph):
        for piece in _hard_split(sentence, words):
            n = _count(piece)
            if current and size + n > words:
                parts.append(" ".join(current))
                current, size = [], 0
            current.append(piece)
            size += n
    if current:
        parts.append(" ".join(current))
    return parts


def _hard_split(sentence: str, words: int) -> list[str]:
    """Parte uma frase maior que `words` em pedaços de `words` unidades (ver `_UNIT`), por
    palavras no texto com espaços e por caracteres no CJK."""
    units = list(_UNIT.finditer(sentence))
    if len(units) <= words:
        return [sentence]
    return [
        sentence[units[i].start() : units[min(i + words, len(units)) - 1].end()]
        for i in range(0, len(units), words)
    ]
