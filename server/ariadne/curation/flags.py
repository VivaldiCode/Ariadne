"""Triagem por flags, determinística (STRUCTURE.md, secção 5, passo 4).

Só o que dispara flags (ou é de saúde, ou cai na amostragem) vai ao curador. Cada flag tem um
código estável e um excerto curto do que a disparou. As regras são deliberadamente simples:
expressões regulares e o HTML; afinam-se com os resultados da amostragem.

Antes das expressões regulares o texto é normalizado (`normalize`): NFKC, sem caracteres de
formato invisíveis, sem acentos nem marcas combinantes, em minúsculas e com os espaços e quebras
de linha colapsados. Assim `ig​nore`, `ＩＧＮＯＲＥ` ou uma quebra de linha a meio não escapam.

Códigos: `injection`, `hidden_text`, `invisible_chars`, `encoded_blob`, `dangerous_command`,
`clinical_dose` e `health` (por fonte ou por palavras-chave).
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import Tag

from ariadne.config import Settings

INJECTION, HIDDEN, ENCODED = "injection", "hidden_text", "encoded_blob"
DANGEROUS, DOSE, HEALTH = "dangerous_command", "clinical_dose", "health"
INVISIBLE = "invisible_chars"
MAX_DETAILS = 3  # excertos guardados por código
MIN_HIDDEN_CHARS = 20  # texto escondido mais curto que isto é lixo de formatação, não ataque
# Texto escondido de prosa que não parece uma instrução só levanta flag se for longo (e não for
# estrutura repetida da fonte): os textos de acessibilidade («By Country Listing of the values for
# the X field»), os JSON de galerias e as coordenadas da Wikipédia têm dezenas de caracteres.
LONG_HIDDEN = 400
SHAPE_WORDS = 5  # as primeiras palavras de um texto escondido identificam o molde da fonte
EXCERPT = 80
MIN_ZERO_WIDTH = 10  # espaços de largura zero a partir dos quais o texto é suspeito

# Texto a casar já normalizado: minúsculas e sem acentos. Os padrões escrevem-se assim.
_INJECTION = [
    re.compile(p)
    for p in (
        r"\b(?:ignore|ignora|ignorez|ignoriere|disregard|forget|override|bypass|esquece|olvida)\b"
        r"[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any|your|anteriores|acima|"
        r"precedentes|previas|vorherigen|obigen|bisherigen)\b[^.\n]{0,30}\b(?:instructions?|"
        r"prompts?|rules|guidelines|instrucoes|instrucciones|regras|reglas|anweisungen|"
        r"instruktionen|consignes)\b",
        r"\b(?:ignora|ignore|ignorez|esquece|olvida|desconsidera)\b[^.\n]{0,30}\b(?:instrucoes|"
        r"instrucciones|instructions|consignes|regras|reglas)\b[^.\n]{0,20}\b(?:anteriores|acima|previas|"
        r"precedentes)\b",
        r"\bdisregard\b[^.\n]{0,20}\b(?:what|everything|anything|all)\b[^.\n]{0,20}\byou\b"
        r"[^.\n]{0,20}\b(?:were|was|have been|had been)\b[^.\n]{0,10}\btold\b",
        r"\bforget\s+(?:everything|all|what)\b[^.\n]{0,30}\b(?:above|before|you)\b",
        r"\bne\s+tiens?\s+pas\s+compte\s+des?\s+(?:instructions|consignes)\b",
        r"\bnote\s+to\s+(?:the\s+)?(?:ai|llm|llms|assistants?|models?|agents?|chatbots?)\b",
        r"\b(?:attention|important|hey|dear)[,:]?\s+(?:ai|llm|assistant|agent|chatbot)s?\b",
        r"</?\s*(?:system|assistant|developer)\s*>",
        r"\[/?inst\]|<<\s*/?sys\s*>>|<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>",
        r"\b(?:system|developer)\s+(?:prompt|message)\s*:",
        r"\byou\s+are\s+now\s+(?:a|an|in|the|my|no longer)\b",
        r"\b(?:pretend|act)\s+(?:to\s+be|that\s+you\s+are|as\s+if\s+you\s+are)\b",
        r"\b(?:new|updated)\s+instructions?\s*:",
        r"\breveal\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)\b",
        r"\bwhen\s+(?:an?\s+)?(?:ai|llm|assistant|language model|agent)\b[^.\n]{0,60}"
        r"\b(?:read|summari[sz]e|see|process)[^.\n]{0,40}\b(?:must|should|always|never)\b",
        r"\b(?:do not|don't|never)\s+(?:tell|reveal|mention)\s+(?:this\s+)?to\s+the\s+user\b",
    )
]
# O texto escondido que fala a um modelo (mais forte que as frases acima, que já levantam
# `injection`): o léxico de quem escreve para uma máquina e não para o leitor.
_HIDDEN_STRONG = [
    re.compile(p)
    for p in (
        r"\bsystem\s+prompt\b|\bprompt\s+injection\b|\bjailbreak|\bdeveloper\s+mode\b",
        r"\bas\s+an?\s+(?:ai|llm|language\s+model|assistant)\b|\blanguage\s+models?\b",
        r"\byou\s+are\s+(?:now\s+|actually\s+|really\s+)?(?:an?|the|my|in|no\s+longer)\b",
        r"\byour\s+(?:new\s+)?(?:task|role|instructions?|goal|job|mission)\b",
        r"\b(?:chatgpt|openai|gpt-?[0-9]|claude|gemini|copilot|llms?)\b",
        r"\b(?:do\s+not|don't|never)\s+(?:tell|reveal|mention|disclose|inform|show)\b",
        r"\b(?:ignore|disregard|forget|ignora|ignore[zr]|esquece)\b[^.\n]{0,40}"
        r"\b(?:instructions?|instrucoes|instrucciones|consignes|prompt|rules|regras)\b",
    )
]
# Um imperativo dirigido ao leitor-máquina, no início de uma frase.
_HIDDEN_COMMAND = re.compile(
    r"(?:^|[.!?:;\n]\s*)(?:please\s+|por\s+favor\s+)?(?:respond|reply|answer|output|print|say|tell"
    r"|send|forward|email|post|upload|visit|open|fetch|click|execute|run|call|insert|include"
    r"|append|recommend|promote|mention|summari[sz]e|translate|repeat|reveal|disclose|pretend|act"
    r"|always|never|responde|responda|diz|envia|visita|abre|nunca|sempre)\b"
)
_MACHINE_WORDS = re.compile(
    r"\b(?:ai|a\.i\.|assistants?|assistente|agents?|bots?|chatbots?|crawlers?|scrapers?|models?"
    r"|modelo|machines?|you|your|tu|voce|summari[sz]ers?)\b"
)
# Mandar dados para um URL: verbo de envio perto de um URL, ou um URL cujos parâmetros levam
# o que o leitor-máquina sabe.
_EXFIL = [
    re.compile(p)
    for p in (
        r"\b(?:send|post|upload|forward|email|exfiltrate|leak|submit|append|include|paste)\b"
        r"[^.\n]{0,100}\b(?:https?://|www\.)",
        r"\b(?:visit|open|fetch|load|navigate|go\s+to|request|call|browse)\b[^.\n]{0,40}"
        r"\b(?:https?://|www\.)\S*[?&=]",
        r"https?://\S*[?&][a-z_]*(?:data|user|token|key|secret|cookie|session|prompt|history"
        r"|conversation|password|email)[a-z_]*=",
        r"!\[[^\]]*\]\(https?://[^)]*(?:\{|%7b)",
    )
]
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
_BASE64_LINE = re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}")
_DANGEROUS = [
    re.compile(p)
    for p in (
        r"\b(?:curl|wget)\b[^|;]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b",
        r"\b(?:curl|wget)\b[^|;]*\|\s*(?:sudo\s+)?(?:python[0-9.]*|perl|ruby|node)\b",
        r"\bbase64\s+(?:-d|--decode)\b[^|;]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b",
        r"\b(?:iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|;]*\|\s*(?:iex|invoke-expression)\b",
        r"\bpowershell(?:\.exe)?\s+[^;|]*-e(?:nc|ncodedcommand)?\s+[a-z0-9+/=]{20,}",
        r"\brm\s+-[a-z]*(?:r[a-z]*f|f[a-z]*r)[a-z]*\s+(?:--no-preserve-root\s+)?(?:/|~|\$home\b)"
        r"(?:\s|\*|/|$)",
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        r"\bmkfs(?:\.\w+)?\s+/dev/",
        r"\bdd\s+if=\S+\s+of=/dev/(?:sd|nvme|hd|disk)",
        r"\bchmod\s+-r\s+0?777\s+/(?:\s|$)",
        r">\s*/dev/sd[a-z]\b",
        r"\bnc\s+(?:-\w+\s+)*-e\s+/bin/(?:ba)?sh\b|/dev/tcp/\d+\.\d+\.\d+\.\d+/\d+",
    )
]
_QUANTITY = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?(?:mg/kg|mg/dl|mcg|µg|μg|mg|ml|ui|iu|meq|mmol(?:/l)?|mmhg)\b"
)
_STRONG_DOSE = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:mg/kg|mcg/kg|ug/kg)\b")
# Sem contexto clínico, «5 mg» ou «5 ml» é uma receita, um rótulo, química: não conta.
_CLINICAL_CONTEXT = re.compile(
    r"\b(?:dose|doses|dosage|dosing|dosagem|dosis|posologia|posology|administer(?:ed|ing)?|"
    r"administrar|administracao|administration|tablets?|comprimidos?|capsules?|capsulas?|"
    r"prescri\w+|patients?|pacientes?|per day|a day|daily|por dia|diari\w+|"
    r"every \d+ (?:hours|h)|a cada \d+ horas|intravenous\w*|intravenos\w+|oral(?:ly|mente)?|"
    r"injection|injecao|inyeccion|twice|duas vezes|tres vezes|three times)\b"
)
_CONTEXT_WINDOW = 120
_CODE_TAGS = ["pre", "code", "kbd", "samp"]
_SPACES = re.compile(r"\s+")
_ZERO_WIDTH = {"​", "⁠", "﻿"}
_BIDI = set("‪‫‬‭‮⁦⁧⁨⁩")


@dataclass(frozen=True)
class Flag:
    code: str
    details: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "details": list(self.details)}


def normalize(text: str) -> str:
    """O texto como as expressões regulares o veem (ver o cabeçalho do módulo)."""
    text = unicodedata.normalize("NFKD", text)
    kept = (c for c in text if unicodedata.category(c) not in ("Cf", "Mn", "Cc") or c in "\n\t")
    return _SPACES.sub(" ", "".join(kept)).casefold().strip()


def _excerpt(text: str, start: int, end: int) -> str:
    lo = max(0, start - 10)
    return text[lo : min(len(text), end + 10)][:EXCERPT]


def _matches(patterns: Iterable[re.Pattern[str]], text: str) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            found.append(_excerpt(text, match.start(), match.end()))
            if len(found) >= MAX_DETAILS:
                return found
    return found


def injection_flags(text: str) -> list[str]:
    """Excertos de `text` que parecem instruções a um modelo (usado também na saída do MCP)."""
    return _matches(_INJECTION, normalize(text))


def invisible_chars(text: str) -> str | None:
    """Caracteres de controlo de direção (bidi), da zona Tag (U+E0000) ou muitos espaços de
    largura zero: servem para esconder texto de quem lê. ZWJ/ZWNJ, comuns em várias escritas,
    não contam."""
    zero = sum(1 for c in text if c in _ZERO_WIDTH)
    if any(c in _BIDI for c in text):
        return "bidirectional control characters (bidi)"
    if any("\U000e0000" <= c <= "\U000e007f" for c in text):
        return "characters from the Tag block (U+E0000)"
    if zero >= MIN_ZERO_WIDTH:
        return f"{zero} zero-width spaces"
    return None


def code_texts(root: BeautifulSoup | Tag) -> list[str]:
    return [t.get_text() for t in root.find_all(_CODE_TAGS)]


# Um URL ou um caminho não é um blob codificado, por mais comprido que seja o troço aleatório do
# fim (`…/serve/MUIEAJWIw9w82Dl4ua6F…`, `…/en/department-of-justice-ho…`): sai antes de procurar.
_URLISH = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.|(?<![\w/])\.{0,2}/(?=\w)|\b[\w-]+(?:\.[\w-]+)+/)"
    r"[^\s<>()\"'\]]*",
    re.IGNORECASE,
)
_MD_TARGET = re.compile(r"\]\([^)\s]*\)")


def strip_urls(text: str) -> str:
    """O texto sem os URLs e os caminhos (o destino dos links Markdown também): o que a procura de
    blobs codificados não deve ver."""
    return _URLISH.sub(" ", _MD_TARGET.sub("] ", text))


def hidden_shape(text: str) -> str:
    """O molde de um texto escondido: as suas primeiras palavras, normalizadas. Os textos que a
    fonte repete com dados diferentes («X This article was co-authored by …») partilham-no."""
    return " ".join(normalize(text).split()[:SHAPE_WORDS])


def is_data(text: str) -> bool:
    """JSON, coordenadas, números: dados da página e não prosa."""
    stripped = text.strip().lstrip("\ufeff").strip()
    if stripped[:1] in "{[" and stripped[-1:] in "}]":
        return True
    if stripped[:1] in "{[":
        return True
    try:
        json.loads(stripped)
        return True
    except ValueError:
        pass
    letters = sum(1 for c in stripped if c.isalpha())
    return letters < 0.5 * max(1, len(stripped.replace(" ", "")))


def instruction_like(text: str) -> bool:
    """O texto parece escrito para um modelo (uma instrução ou injeção, ou um URL para onde enviar
    dados) e não para quem lê a página."""
    normalized = normalize(text)
    if _matches(_INJECTION, normalized) or any(p.search(normalized) for p in _HIDDEN_STRONG):
        return True
    if any(p.search(normalized) for p in _EXFIL):
        return True
    return bool(_HIDDEN_COMMAND.search(normalized) and _MACHINE_WORDS.search(normalized))


def suspicious_hidden(text: str, known: Collection[str] = ()) -> bool:
    """O texto escondido merece a flag `hidden_text`? Sim se parece uma instrução ou injeção
    (`instruction_like`). Não se é JSON ou dados, nem se é a linha ou o molde que a fonte repete
    (`known`: as linhas e os moldes do lixo da fonte). Os textos de acessibilidade e os rótulos
    curtos não contam; só a prosa longa (`LONG_HIDDEN`) que ninguém mais repete."""
    if instruction_like(text):
        return True
    if is_data(text):
        return False
    if known and (normalize(text) in known or hidden_shape(text) in known):
        return False
    return len(normalize(text)) >= LONG_HIDDEN


def _blobs(text: str, minimum: int) -> list[str]:
    text = strip_urls(text)
    found = [m.group(0) for m in _BASE64.finditer(text) if len(m.group(0)) >= minimum]
    run: list[str] = []  # base64 partido em linhas de 64-76 caracteres
    for line in [*text.splitlines(), ""]:
        if _BASE64_LINE.fullmatch(line.strip()):
            run.append(line.strip())
            continue
        if len(run) > 1 and sum(len(r) for r in run) >= minimum:
            found.append("".join(run))
        run = []
    return found


def clinical_doses(normalized: str) -> list[str]:
    """Doses com contexto clínico: `mg/kg` por si só, ou uma quantidade (mg, mcg, ml, UI…) com
    uma palavra de posologia/administração/«por dia» perto."""
    found = [_excerpt(normalized, m.start(), m.end()) for m in _STRONG_DOSE.finditer(normalized)]
    for match in _QUANTITY.finditer(normalized):
        lo, hi = max(0, match.start() - _CONTEXT_WINDOW), match.end() + _CONTEXT_WINDOW
        if _CLINICAL_CONTEXT.search(normalized[lo:hi]):
            found.append(_excerpt(normalized, match.start(), match.end()))
    return found[:MAX_DETAILS]


def detect(
    markdown: str,
    *,
    code: Iterable[str] = (),
    hidden: Iterable[str] = (),
    settings: Settings,
    source_id: str = "",
    known_hidden: Collection[str] = (),
) -> list[Flag]:
    """As flags de um artigo: `markdown` é o texto limpo, `code` os blocos de código do HTML,
    `hidden` o texto que estava escondido (já tirado do `markdown`) e `known_hidden` as linhas e
    os moldes que a fonte repete (`Boilerplate.lines` e `.hidden`): esse texto escondido é
    estrutura. Só o que parece uma instrução, ou é longo e ninguém repete, levanta `hidden_text`
    (`suspicious_hidden`); o texto escondido todo entra na procura de injeções e comandos."""
    flags: list[Flag] = []
    code = list(code)
    hidden = [h for h in hidden if len(normalize(h)) >= MIN_HIDDEN_CHARS]
    raw_all = "\n".join([markdown, *hidden, *code])
    normalized = normalize("\n".join([markdown, *hidden]))
    if found := _matches(_INJECTION, normalized):
        flags.append(Flag(INJECTION, tuple(found)))
    suspicious = [h for h in hidden if suspicious_hidden(h, known_hidden)]
    if suspicious:
        flags.append(Flag(HIDDEN, tuple(h[:EXCERPT] for h in suspicious[:MAX_DETAILS])))
    if reason := invisible_chars(raw_all):
        flags.append(Flag(INVISIBLE, (reason,)))
    blobs = _blobs(raw_all, settings.curation_base64_min_chars)
    if blobs:
        flags.append(Flag(ENCODED, tuple(f"{b[:30]}… ({len(b)} caracteres)" for b in blobs[:2])))
    if found := _matches(_DANGEROUS, normalize("\n".join([markdown, *hidden, *code]))):
        flags.append(Flag(DANGEROUS, tuple(found)))
    if found := clinical_doses(normalized):
        flags.append(Flag(DOSE, tuple(found)))
    if reason := health_reason(markdown, source_id, settings):
        flags.append(Flag(HEALTH, (reason,)))
    return flags


def _split(csv: str) -> list[str]:
    return [normalize(item) for item in csv.split(",") if item.strip()]


def health_reason(markdown: str, source_id: str, settings: Settings) -> str | None:
    """Porque é que o artigo conta como saúde (sempre curado): a fonte, ou palavras-chave."""
    lowered_source = source_id.casefold()
    source = next(
        (s for s in _split(settings.curation_health_sources) if s in lowered_source), None
    )
    if source:
        return f"source {source}"
    text = normalize(markdown)
    words = [w for w in _split(settings.curation_health_keywords) if w in text]
    if len(words) >= settings.curation_health_min_hits:
        return "keywords: " + ", ".join(words[:5])
    return None


def sampled(document_id: str, rate: float) -> bool:
    """Amostragem aleatória mas determinística: o hash do documento cai abaixo de `rate`."""
    if rate <= 0:
        return False
    digest = hashlib.sha256(document_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


def flags_json(flags: Iterable[Flag]) -> list[dict[str, object]]:
    return [flag.as_dict() for flag in flags]
