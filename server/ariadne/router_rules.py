"""Regras simples e explicáveis do router (ARCHITECTURE.md, secções 5 e 8): sem modelo nenhum.

- `classify`: a pergunta é **histórica**? (palavras-chave em PT/EN/ES/FR, anos, datas). Diz sempre
  porquê (`reasons`), para o registo e para quem quiser afinar a lista.
- `detect_language`: a língua da pergunta (pt, en, es, fr, de, it) por palavras funcionais e
  carateres típicos. É uma heurística: devolve `None` quando não se decide.
"""

import re
import unicodedata
from dataclasses import dataclass

# --- pergunta histórica ------------------------------------------------------------------

# Palavras de história **fortes** (sem acentos nem maiúsculas): chega uma para a pergunta ser
# histórica. As de `STEMS` contam por prefixo; as de `WHOLE` só como palavra inteira («coup» não é
# «coupling», «independence» não é «independent variables»).
STEMS = frozenset(
    {
        "revolu",  # revolução, revolution, revolución, révolution
        "guerra", "guerre", "batalh", "battle", "batalla", "bataille",
        "reinado", "reign", "regne", "secul", "century", "centuries", "siglo", "siecle",
        "golpe", "tratado", "treaty", "treaties", "traite", "ditadur", "dictator", "dictadur",
        "monarqu", "monarch", "dinasti", "dynast", "colonial", "coloniza", "descobriment",
        "renascimento", "renaissance", "renacimiento", "medieval", "holocaust", "genocid",
        "massacr", "cruzada", "crusade", "croisade", "conquista", "conquest", "conquete",
        "armisticio", "armistice", "antiguidade", "antiguedad", "antiquite", "historic",
        "historiqu", "historico",
    }
)  # fmt: skip
WHOLE = frozenset({"coup", "independence", "independencia", "independance", "invasao", "invasion"})
# Palavras **fracas**: sozinhas não dizem nada («Star Wars cast», «React Empire app»); precisam de
# outro sinal (outra palavra, uma data, ou uma destas palavras de contexto).
WEAK = frozenset({"war", "wars", "empire", "empires", "imperio", "imperios", "colony", "colonies"})
WEAK_CONTEXT = frozenset(
    {
        "roman", "rome", "roma", "romano", "ottoman", "otomano", "byzantine", "bizantino",
        "napoleonic", "napoleao", "world", "mundial", "civil", "cold", "fria", "hundred",
        "greek", "grego", "ancient", "british", "britanico", "portuguese", "portugues",
    }
)  # fmt: skip
# Frases fracas («browser history» não é história): contam como um sinal.
WEAK_PHRASES = (r"(?:history|historia|histoire)\s+(?:of|de|do|da|dos|das|du|des)\b",)
# Frases fortes.
STRONG_PHRASES = (
    r"middle ages|idade media|edad media|moyen age|ancient (?:rome|greece|egypt|history)",
    r"world war|ww\s?(?:1|2|i|ii)\b",
)
_MONTHS = (
    "janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro "
    "january february march april may june july august september october november december "
    "enero febrero marzo mayo junio julio septiembre setiembre octubre noviembre diciembre "
    "janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre"
)
_MONTH = "|".join(sorted(set(_MONTHS.split()), key=len, reverse=True))
# Um ano (1000–1999) só conta com uma palavra de contexto mesmo antes («em 1755», «in 1755», «de
# 1974»): sem isso «GTX 1080» e «error 1054» seriam história.
_CONTEXT_YEAR = re.compile(
    r"\b(?:in|em|en|no|na|ano|year|an|of|de|do|da|since|desde|depuis|until|ate|before|after|"
    r"antes|depois|durante|during|by|circa|c)\s+(1[0-9]{3})\b"
)
# «44 BC», «476 d.C.», «500 a.C.»: sem «AD» nem «CE» soltos (são palavras inglesas comuns).
_ERA_YEAR = re.compile(r"\b\d{1,4}\s*(?:bce?(?!\w)|[ad]\.\s?c\.|a\.d\.|c\.e\.)")
_NAMED_YEAR = re.compile(r"\b(?:ano|year|an)\s+(\d{3})\b(?!\s*%)")
_DATE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th|o|º)?\s+(?:de\s+|of\s+|d'\s*)?(?:{_MONTH})\b"
    rf"|\b(?:{_MONTH})\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    r"|\b\d{1,2}[/.-]\d{1,2}[/.-]1[0-9]{3}\b|\b1[0-9]{3}-\d{2}-\d{2}\b"
)
_WORD = re.compile(r"[a-z0-9]+")


def fold(text: str) -> str:
    """Minúsculas e sem acentos: «Revolução» -> «revolucao»."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(frozen=True)
class Classification:
    historical: bool
    reasons: tuple[str, ...]  # ex.: ('keyword:revolucao', 'year:1974'); vazio se não é histórica


def classify(query: str) -> Classification:
    """A pergunta é histórica se tem um sinal **forte** (palavra de história como revolução,
    batalha, tratado; um ano com contexto, «44 BC», «ano 476») ou, no mínimo, dois sinais **fracos**
    (guerra/império sem mais nada não chega; nem uma data sozinha; juntos com outro sinal, sim)."""
    folded = fold(query)
    words = list(dict.fromkeys(_WORD.findall(folded)))
    strong: list[str] = []
    weak: list[str] = []
    for word in words:
        if word in WHOLE or any(word.startswith(stem) for stem in STEMS):
            strong.append(f"keyword:{word}")
        elif word in WEAK:
            weak.append(f"weak:{word}")
        elif word in WEAK_CONTEXT:
            weak.append(f"context:{word}")
    for pattern in STRONG_PHRASES:
        if match := re.search(pattern, folded):
            strong.append(f"keyword:{match.group(0)}")
    for pattern in WEAK_PHRASES:
        if match := re.search(pattern, folded):
            weak.append(f"phrase:{match.group(0)}")
    strong += [f"year:{m.group(1)}" for m in _CONTEXT_YEAR.finditer(folded)]
    strong += [f"year:{m.group(0).strip()}" for m in _ERA_YEAR.finditer(folded)]
    strong += [f"year:{m.group(1)}" for m in _NAMED_YEAR.finditer(folded)]
    weak += [f"date:{m.group(0)}" for m in _DATE.finditer(folded)]
    has_weak_word = any(w.startswith("weak:") for w in weak)
    # Uma palavra fraca precisa de mais um sinal; sem palavra fraca, duas datas/frases/contextos
    # também não chegam («25 de abril» sozinha não é história).
    historical = (
        bool(strong)
        or (len(weak) >= 2 and has_weak_word)
        or (len([w for w in weak if w.startswith(("date:", "phrase:"))]) >= 2)
    )
    if not historical:
        return Classification(False, ())
    return Classification(True, tuple(dict.fromkeys([*strong, *weak])))


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# --- língua da pergunta ------------------------------------------------------------------

STOPWORDS: dict[str, frozenset[str]] = {
    "pt": _words(
        "o a os as um uma uns umas de do da dos das em no na nos nas por para com sem sobre entre "
        "que qual quais quem como quando onde porque foi era são ser é está estão há ao aos à às "
        "se não mais também ou e mas já muito seu sua seus suas meu minha este esta isto isso "
        "aquele aquela depois antes durante"
    ),
    "en": _words(
        "the a an of in on at to for with without about between what which who whom how when where "
        "why was were is are be been has have had did does do and or but not also very this that "
        "these those it its his her their my your from by after before during into over under"
    ),
    "es": _words(
        "el la los las un una unos unas de del en por para con sin sobre entre que cual cuales "
        "quien como cuando donde porque fue era son ser es esta estan hay al se no mas tambien o y "
        "pero ya muy su sus mi este esta eso aquel despues antes durante"
    ),
    "fr": _words(
        "le la les un une des du de en dans sur pour avec sans entre que quel quelle quels qui "
        "comment quand ou pourquoi fut etait sont etre est a au aux et mais pas aussi tres ce cet "
        "cette ces son sa ses mon ma apres avant pendant"
    ),
    "de": _words(
        "der die das den dem des ein eine einen einem und oder aber nicht auch sehr ist sind war "
        "waren wer was wie wann wo warum von zu mit ohne uber zwischen nach vor wahrend"
    ),
    "it": _words(
        "il lo la i gli le un uno una di del della dei delle in su per con senza tra fra che quale "
        "chi come quando dove perche fu era sono essere e ma non anche molto questo questa dopo "
        "prima durante"
    ),
}
# Palavras comuns a várias línguas não decidem nada: fora da contagem de cada uma onde colidem.
_AMBIGUOUS = frozenset({"a", "e", "o", "en", "es", "de", "no", "se", "la", "que", "me", "un"})
_HINTS = {"pt": "ãõç", "es": "ñ¿¡", "fr": "èêëœùûôîâ", "de": "äöüß"}


def detect_language(text: str, *, ascii_default: str | None = None) -> str | None:
    """A língua mais provável de `text` entre pt, en, es, fr, de, it; `None` se não se decide.

    Conta palavras funcionais de cada língua e dá peso a carateres típicos (ã, ñ, ß…). Um texto só
    com ASCII e sem nenhuma palavra funcional conhecida («Lisboa», «gato», «Carnation Revolution»)
    não se decide (`None`, ou `ascii_default` se o chamador quiser adivinhar)."""
    lowered = text.casefold()
    words = _WORD.findall(fold(text))
    scores: dict[str, float] = {}
    for language, stopwords in STOPWORDS.items():
        hits = sum(1.0 if w not in _AMBIGUOUS else 0.4 for w in words if w in stopwords)
        hits += sum(2.0 for c in lowered if c in _HINTS.get(language, ""))
        if hits:
            scores[language] = hits
    if not scores:
        return ascii_default if lowered.isascii() else None
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.5:
        return None
    return ranked[0][0]
