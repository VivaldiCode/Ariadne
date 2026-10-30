"""Verificação de fidelidade, determinística (STRUCTURE.md, secção 5, passo 8).

Compara o texto original (já limpo) com a reescrita do curador. Extrai quatro tipos de factos:

- **quantidades**: número + unidade (`5 mg`, `12 km`, `3 %`, `3 dias`, `2 vezes`, `1 comprimido`,
  um só algarismo incluído, porque aí a unidade dá-lhe contexto) e **intervalos** (`5-10 mg` é um
  par, comparado como tal): tolerância zero, nada novo e nada a menos (uma dose que muda ou
  desaparece é o erro mais caro). As unidades comparam-se sem maiúsculas (`MG` = `mg`);
- **números** soltos (anos, contagens, dias);
- **meses** (por extenso, em seis línguas);
- **nomes próprios**: palavras com maiúscula que não começam uma frase.

As unidades de tempo normalizam-se (`15 min` = `15 minutes` = `15 minutos`): reescrever «min» por
«minutes» não é perder um número. Um mês só conta se for mesmo uma data («May 5»; o «may» do
inglês é um verbo). O título do documento (`context`) não conta como nome acrescentado: um
curador que o repete no resumo não inventou nada. Um nome só se perdeu se a palavra desapareceu
do texto: «Olive Oil» que passa a «olive oil» perdeu a maiúscula e nada mais.

Nos números, meses e nomes, a reescrita **não pode acrescentar nada** que o original não tenha
(seria inventado; um nome que o original já usava, mesmo no início de uma frase, não conta como
acrescentado) e pode perder no máximo `CURATION_FIDELITY_TOLERANCE` (10%) do que o original
tinha, porque um curador legítimo corta rodapés e repetições. Os números de um só algarismo sem
unidade não contam (marcadores de lista e de nota). Os números normalizam-se: `1,000` e `1.000`
são o mesmo que `1000`; `3,5` e `3.5` também.

Se falhar, o item não passa: vai para a revisão humana com o relatório (`FidelityReport`).
"""

import re
from dataclasses import dataclass, field
from typing import Any

UNITS = (
    "mg/kg|mg/dl|mg|mcg|µg|μg|kg|g|ml|dl|l|mmol|meq|iu|ui|mmhg|km/h|km|m|cm|mm|ha|%|°c|°f|"
    "mph|kcal|cal|kb|mb|gb|tb|hz|khz|mhz|ghz|kw|bpm|"
    "anos?|years?|meses|months?|semanas?|weeks?|dias?|days?|horas?|hours?|hrs?|minutos?|minutes?|mins?|"
    "segundos?|seconds?|secs?|"
    "vezes|veces|times|doses?|comprimidos?|tablets?|c[aá]psulas?|capsules?|ampolas?|gotas?|drops?"
)
_NUMBER = r"\d+(?:[.,   ]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_QUANTITY = re.compile(
    rf"(?<![\w.,])({_NUMBER})(?:\s?[-–—]\s?({_NUMBER}))?\s?({UNITS})(?![\w])", re.IGNORECASE
)
_NUMBER_RE = re.compile(rf"(?<![\w.,])({_NUMBER})(?![\w])")
_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "janeiro", "fevereiro", "março", "abril", "maio",
    "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro", "enero",
    "febrero", "marzo", "mayo", "junio", "julio", "septiembre", "octubre", "noviembre",
    "diciembre", "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
    "septembre", "octobre", "novembre", "décembre", "januar", "februar", "märz", "juni", "juli",
    "oktober", "dezember", "gennaio", "febbraio", "aprile", "maggio", "giugno", "luglio",
    "settembre", "ottobre", "dicembre",
}  # fmt: skip
_AMBIGUOUS_MONTHS = frozenset({"may", "march", "mai", "mars"})  # também são palavras comuns
_UNIT_ALIASES = {
    "min": "min", "mins": "min", "minute": "min", "minutes": "min", "minuto": "min",
    "minutos": "min", "hr": "h", "hrs": "h", "hour": "h", "hours": "h", "hora": "h",
    "horas": "h", "sec": "s", "secs": "s", "second": "s", "seconds": "s", "segundo": "s",
    "segundos": "s", "day": "d", "days": "d", "dia": "d", "dias": "d", "week": "w",
    "weeks": "w", "semana": "w", "semanas": "w", "year": "y", "years": "y", "ano": "y",
    "anos": "y", "month": "mo", "months": "mo", "meses": "mo",
}  # fmt: skip
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_NAME = re.compile(r"(?<![\w])([A-ZÀ-ÖØ-Þ][\wà-öø-ÿ'’-]+)")
_WORD_AT = re.compile(r"[^\W\d_]+", re.UNICODE)
_DATE_NEIGHBOUR = re.compile(r"(?:\d{1,4}\s*(?:st|nd|rd|th|de)?\s*$)|(?:^\s*(?:de\s+)?\d{1,4})")
_SENTENCE_START = re.compile(r"(?:^|[.!?¡¿:;]\s+|\n\s*(?:[-*#>\d.]+\s*)?)$")
_MARKUP = re.compile(r"```.*?```|`[^`\n]*`|\]\([^)\s]*\)|https?://\S+", re.DOTALL)


def _is_date(text: str, match: re.Match[str]) -> bool:
    """«May 5», «5 May», «de maio de 2020»: o mês ambíguo tem maiúscula e um número ao lado."""
    word = match.group(0)
    if not word[:1].isupper():
        return False
    before, after = (
        text[max(0, match.start() - 8) : match.start()],
        text[match.end() : match.end() + 8],
    )
    return bool(_DATE_NEIGHBOUR.search(before) or _DATE_NEIGHBOUR.search(after))


@dataclass
class Facts:
    quantities: set[str] = field(default_factory=set)
    numbers: set[str] = field(default_factory=set)
    months: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)


def _normalize_number(raw: str) -> str:
    text = re.sub(r"[   ]", "", raw)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):  # separador de milhares
        return re.sub(r"[.,]", "", text)
    return text.replace(",", ".")


def extract(text: str) -> Facts:
    text = _MARKUP.sub(" ", text)
    facts = Facts()
    taken: list[tuple[int, int]] = []
    for match in _QUANTITY.finditer(text):
        number = _normalize_number(match.group(1))
        if match.group(2):  # intervalo: «5-10 mg» é um par
            number = f"{number}-{_normalize_number(match.group(2))}"
        unit = match.group(3).casefold()
        facts.quantities.add(f"{number}{_UNIT_ALIASES.get(unit, unit)}")
        taken.append(match.span())
    for match in _NUMBER_RE.finditer(text):
        if any(lo <= match.start() < hi for lo, hi in taken):
            continue
        number = _normalize_number(match.group(1))
        if len(number) > 1:
            facts.numbers.add(number)
    for match in _WORD_AT.finditer(text):
        word = match.group(0).casefold()
        if word not in _MONTHS:
            continue
        if word in _AMBIGUOUS_MONTHS and not _is_date(text, match):
            continue  # «you may add» não é maio
        facts.months.add(word)
    for match in _NAME.finditer(text):
        window = text[max(0, match.start() - 8) : match.start()]
        if match.start() <= 8:  # o princípio do texto é o princípio de uma frase («# Granola»)
            window = "\n" + window
        if _SENTENCE_START.search(window):
            continue  # a maiúscula é do início da frase, não prova nada
        facts.names.add(match.group(1).casefold())
    return facts


@dataclass
class FidelityReport:
    ok: bool
    problems: list[str]
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": self.problems, **self.detail}


def check(original: str, rewritten: str, tolerance: float, context: str = "") -> FidelityReport:
    """Compara os factos dos dois textos (ver a regra no cabeçalho do módulo). `context` é texto
    que o original não repete mas que o curador também viu (o título): as suas palavras não são
    nomes acrescentados."""
    before, after = extract(original), extract(rewritten)
    known_words = set(_WORD.findall(original.casefold())) | set(_WORD.findall(context.casefold()))
    rewritten_words = set(_WORD.findall(rewritten.casefold()))
    problems: list[str] = []
    detail: dict[str, Any] = {"tolerance": tolerance}
    for kind, strict in (
        ("quantities", True),
        ("numbers", False),
        ("months", False),
        ("names", False),
    ):
        old, new = getattr(before, kind), getattr(after, kind)
        added, missing = sorted(new - old), sorted(old - new)
        if kind == "names":  # uma maiúscula nova não faz um nome novo se a palavra já existia
            added = [name for name in added if name not in known_words]
            # e um nome que só perdeu a maiúscula («Olive Oil» → «olive oil») não se perdeu
            missing = [name for name in missing if name not in rewritten_words]
        detail[kind] = {"added": added[:20], "missing": missing[:20]}
        label = {
            "quantities": "quantities",
            "numbers": "numbers",
            "months": "months",
            "names": "names",
        }[kind]
        if added:
            problems.append(f"{label} added: {', '.join(added[:5])}")
        lost = len(missing) / len(old) if old else 0.0
        if missing and (strict or lost > tolerance):
            problems.append(f"{label} lost: {', '.join(missing[:5])} ({lost:.0%})")
    return FidelityReport(not problems, problems, detail)
