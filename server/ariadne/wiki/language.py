"""Língua de cada página (docs/WIKI-IMPORT.md, secção 5).

A língua do ZIM é só o ponto de partida: o wikem traz páginas `/zh` e `/it`, o gentoo `/ja`, `/ko`
e `/fr`, o wiki do OSM `FR:` e `Zh-hans:`, o docs.python `ko/3.12/`, e o `<html lang>` falha dos
dois lados (todas as páginas do wikem dizem `en`, as do ifixit dizem `fr`). Por isso a ordem é:

1. o que a página declara (`<html lang>`/`<body lang>`) e o que o caminho diz (sufixo `/zh`,
   prefixo `FR:`, segmento `ko/3.12/`), por esta ordem, com uma exceção: um `lang` igual à língua
   do ZIM não informa nada (é o valor por omissão), por isso o caminho passa à frente;
2. cada candidato só vale se o detetor não o contradisser com confiança. O detetor olha para a
   escrita (hangul, kana, cirílico…) e, nas línguas latinas, para palavras funcionais frequentes
   (pt, en, es, fr, de, it, nl), sem dependências novas;
3. se todos forem contradizidos, vale o detetor; se o texto não chega, a língua do ZIM.

O resultado é ISO 639-1 (`pt`, `en`…), ou o código de 3 letras do ZIM numa língua que não tem
de 2 (`ceb`); `und` se nada indicar uma língua.

**Da língua ao locale do Wiki.js** (`wiki_locale`). O Wiki.js 2.5 só publica em locales do seu
catálogo (`WIKIJS_CATALOG`, 70) e só os encaminha se o código for `xx` ou `xx-yy` (o regex do seu
`page.js` é `^[A-Z]{2}(-[A-Z]{2})?$`: um código de 3 letras ou `sr-latn` não funciona). A regra:

1. cada língua ISO 639-1 vai para o locale do catálogo que a representa: o próprio código
   (`fr`), ou o único que o catálogo tem (`is` → `is-is`, `hi` → `hi-in`); `no`/`nor` → `nb`;
2. os códigos de 3 letras (ISO 639-3 e 639-2/B: `por`, `fra`, `swa`) convertem-se primeiro para
   ISO 639-1 (`ISO3_TO_1`); uma língua que só existe em 3 letras (`ceb`, `war`, `arz`…) ou que
   o catálogo não tem (`mg`, `cy`…) **não tem locale**: a página fica indexada no Ariadne, não se
   publica (`meta.wiki_skip`) e conta-se no relatório;
3. as variantes de região ou de escrita juntam-se à língua de base (`pt-PT`, `en-GB`, `sr-Latn`
   → `pt`, `en`, `sr`), exceto as duas que o catálogo trata à parte: `pt-BR` → `pt-br` e o chinês
   tradicional (`zh-Hant`, `zh-TW`, `zh-HK`, `zh-MO`) → `zh-tw`; o resto do chinês é `zh`.
"""

import re
from collections import Counter
from collections.abc import Sequence
from unicodedata import name as unicode_name
from urllib.parse import unquote

from ariadne.sources.zim import LANGUAGE_2

UNDETERMINED = "und"


def _vocabulary(words: str) -> frozenset[str]:
    return frozenset(words.split())


# Códigos ISO 639-1 que aparecem em caminhos de wikis e de documentação traduzida, com as
# variantes de escrita e de região que o MediaWiki e o Sphinx usam.
_CODES = _vocabulary(
    "af am ar az be bg bn bs ca cs cy da de el en eo es et eu fa fi fr ga gl he hi hr hu hy id is "
    "it ja ka kk km kn ko ky la lt lv mk ml mn mr ms my nb ne nl nn no pa pl pt ro ru si sk sl sq "
    "sr sv sw ta te th tl tr uk ur uz vi zh"
)
# Códigos que também são palavras comuns: só valem como prefixo `XX:` ou com versão a seguir.
_AMBIGUOUS_CODES = frozenset({"is", "no", "my", "te", "la", "ar", "si", "ne", "id", "to", "as"})
_VARIANTS = re.compile(r"^([a-z]{2})[-_](?:[a-z]{2,4})$")  # zh-hans, pt-br, sr-latn, zh_CN
_VERSION = re.compile(r"^v?\d+(?:\.\d+)*[a-z]?$")
_PREFIX = re.compile(r"^([a-z]{2}(?:[-_][a-z]{2,4})?):\S", re.IGNORECASE)

_LANG_ATTR = re.compile(
    r"<(?:html|body)\b[^>]*?\b(?:xml:)?lang\s*=\s*[\"']?([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)?)",
    re.IGNORECASE,
)


def to_iso1(code: str | None) -> str | None:
    """Normaliza qualquer código (`por`, `pt-BR`, `EN_US`, `zh-Hans`) para ISO 639-1; None se não
    se reconhece (inclui as línguas que só têm código de 3 letras). Nas listas de línguas dos ZIM
    multilingues (`eng,fra,…`) não adivinha: None."""
    if not code or "," in code:
        return None
    base = re.split(r"[-_]", code.strip().lower(), maxsplit=1)[0]
    if len(base) == 3:
        return ISO3_TO_1.get(base) or LANGUAGE_2.get(base)
    return base if base in ISO1 else None


def declared_language(html: str | None) -> str | None:
    """A língua que a página diz ter (`<html lang>`, senão `<body lang>`), em ISO 639-1."""
    if not html:
        return None
    for match in _LANG_ATTR.finditer(html[:20_000]):
        language = to_iso1(match.group(1))
        if language is not None:
            return language
    return None


def path_language(path: str) -> str | None:
    """A língua que o caminho indica: `…/zh`, `FR:Key:conveying`, `Zh-hans:Tag`, `…/ko/3.12/…`."""
    segments = [s for s in unquote(path).strip("/").split("/") if s]
    if not segments:
        return None
    last = segments[-1]
    prefix = _PREFIX.match(last)
    if prefix is not None:  # OSM: `FR:Key:conveying`
        language = to_iso1(prefix.group(1))
        if language is not None:
            return language
    code = _segment_code(last) if last == last.lower() else None  # wikem: `…/zh`
    if code is not None and code not in _AMBIGUOUS_CODES:
        return code
    for index, segment in enumerate(segments[:-1]):  # docs.python: `docs.python.org/ko/3.12/…`
        if segment != segment.lower() or (code := _segment_code(segment)) is None:
            continue
        after_host = index == 0 or "." in segments[index - 1]
        if _VERSION.match(segments[index + 1]) or (after_host and code not in _AMBIGUOUS_CODES):
            return code
    return None


def _segment_code(segment: str) -> str | None:
    variant = _VARIANTS.match(segment)
    base = variant.group(1) if variant else segment
    return base if base in _CODES else None


# --- da língua ao locale do Wiki.js ---------------------------------------------------------------

# ISO 639-3 e 639-2/B (as duas formas que os ZIM e o Calibre usam) -> ISO 639-1, as 184 línguas que
# têm código de 2 letras (gerada com o pycountry; sem dependência nova no servidor).
_ISO3_PAIRS = (
    "aar=aa abk=ab afr=af aka=ak alb=sq amh=am ara=ar arg=an arm=hy asm=as ava=av ave=ae aym=ay "
    "aze=az bak=ba bam=bm baq=eu bel=be ben=bn bis=bi bod=bo bos=bs bre=br bul=bg bur=my cat=ca "
    "ces=cs cha=ch che=ce chi=zh chu=cu chv=cv cor=kw cos=co cre=cr cym=cy cze=cs dan=da deu=de "
    "div=dv dut=nl dzo=dz ell=el eng=en epo=eo est=et eus=eu ewe=ee fao=fo fas=fa fij=fj fin=fi "
    "fra=fr fre=fr fry=fy ful=ff geo=ka ger=de gla=gd gle=ga glg=gl glv=gv gre=el grn=gn guj=gu "
    "hat=ht hau=ha hbs=sh heb=he her=hz hin=hi hmo=ho hrv=hr hun=hu hye=hy ibo=ig ice=is ido=io "
    "iii=ii iku=iu ile=ie ina=ia ind=id ipk=ik isl=is ita=it jav=jv jpn=ja kal=kl kan=kn kas=ks "
    "kat=ka kau=kr kaz=kk khm=km kik=ki kin=rw kir=ky kom=kv kon=kg kor=ko kua=kj kur=ku lao=lo "
    "lat=la lav=lv lim=li lin=ln lit=lt ltz=lb lub=lu lug=lg mac=mk mah=mh mal=ml mao=mi mar=mr "
    "may=ms mkd=mk mlg=mg mlt=mt mon=mn mri=mi msa=ms mya=my nau=na nav=nv nbl=nr nde=nd ndo=ng "
    "nep=ne nld=nl nno=nn nob=nb nor=no nya=ny oci=oc oji=oj ori=or orm=om oss=os pan=pa per=fa "
    "pli=pi pol=pl por=pt pus=ps que=qu roh=rm ron=ro rum=ro run=rn rus=ru sag=sg san=sa sin=si "
    "slk=sk slo=sk slv=sl sme=se smo=sm sna=sn snd=sd som=so sot=st spa=es sqi=sq srd=sc srp=sr "
    "ssw=ss sun=su swa=sw swe=sv tah=ty tam=ta tat=tt tel=te tgk=tg tgl=tl tha=th tib=bo tir=ti "
    "ton=to tsn=tn tso=ts tuk=tk tur=tr twi=tw uig=ug ukr=uk urd=ur uzb=uz ven=ve vie=vi vol=vo "
    "wel=cy wln=wa wol=wo xho=xh yid=yi yor=yo zha=za zho=zh zul=zu "
)
ISO3_TO_1 = dict(pair.split("=", 1) for pair in _ISO3_PAIRS.split())
ISO1 = frozenset(ISO3_TO_1.values())

# Os locales do catálogo do Wiki.js 2.5 (`localization.locales`, 2026-10-28). `sr-latn` fica de fora
# do que se publica: o encaminhamento do Wiki.js não o reconhece (ver o texto do módulo).
WIKIJS_CATALOG = frozenset(
    _vocabulary(
        "af am ar hy as az eu-es bn bs-ba bg ca zh zh-tw hr cs da nl en eo et fi fr gl de el gu-in "
        "ht-ht he hi-in hu is-is id ga-ie it ja kn kk km-kh ko lv lt mk-mk ml mr mn nb nn or fa pl "
        "pt pt-br pa ro ru sr sr-latn si sk sl es sv ta te th tr uk ur ug vi"
    )
)
_ROUTABLE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
WIKIJS_LOCALES = frozenset(code for code in WIKIJS_CATALOG if _ROUTABLE.match(code))

# ISO 639-1 -> locale: o próprio código, ou o único que o catálogo tem para a língua.
_LOCALE_OF: dict[str, str] = {}
for _code in sorted(WIKIJS_LOCALES, key=lambda code: ("-" in code, code)):
    _LOCALE_OF.setdefault(_code.split("-")[0], _code)
_LOCALE_OF["no"] = "nb"  # «norueguês» sem mais: o bokmål (o catálogo não tem `no`)
# (língua, região ou escrita) -> locale à parte.
_VARIANT_LOCALES = {
    ("pt", "br"): "pt-br",
    ("zh", "hant"): "zh-tw",
    ("zh", "tw"): "zh-tw",
    ("zh", "hk"): "zh-tw",
    ("zh", "mo"): "zh-tw",
}
_TAG = re.compile(r"^([a-z]{2,3})[-_]([a-z0-9]{2,8})", re.IGNORECASE)


def wiki_locale(language: str | None, tags: Sequence[str | None] = ()) -> str | None:
    """O locale do Wiki.js para uma língua (ISO 639-1 ou de 3 letras), ou None se não tem (ver o
    texto do módulo). `tags` são as etiquetas brutas onde a página ou o ZIM podem trazer a região
    ou a escrita (`pt-BR`, `zh-Hant`): a primeira que decide a variante ganha."""
    base = to_iso1(language)
    if base is None:
        return None
    for raw in tags:
        match = _TAG.match((raw or "").strip())
        if match is None or to_iso1(match.group(1)) != base:
            continue
        variant = _VARIANT_LOCALES.get((base, match.group(2).lower()))
        if variant is not None:
            return variant
    return _LOCALE_OF.get(base)


def declared_tag(html: str | None) -> str | None:
    """A etiqueta tal como a página a declara (`pt-BR`, `zh-Hant`), para a variante do locale."""
    if not html:
        return None
    match = _LANG_ATTR.search(html[:20_000])
    return match.group(1) if match else None


# --- o detetor -----------------------------------------------------------------------------------

# Palavras funcionais frequentes. Ficam de fora as que se repartem por várias línguas (de, la, le,
# en, que, se, in, a, e, o…): cada lista tem só o que a distingue das outras.
_FUNCTION_WORDS: dict[str, frozenset[str]] = {
    "pt": _vocabulary(
        "não nao em uma uns umas dos das nos nas pelo pela pelos pelas ao aos seu sua seus suas "
        "também tambem já foram são sao está estão tem têm há mais mas como com para por sobre "
        "entre depois até quando muito isso esse essa este esta nós vocês ele ela eles elas "
        "então onde sem ser foi era ou os as"
    ),
    "en": _vocabulary(
        "the of and to is that for with as was on are by this be it from or an at which not have "
        "has had but were their they his her also been its more other than these can will would "
        "who one all there when what about into some such only over after"
    ),
    "es": _vocabulary(
        "el los las del una unos unas por con para como más pero sus ya fue son está están este "
        "esta estos estas también entre sobre cuando muy sin hasta desde porque donde fueron "
        "puede pueden ser tiene tienen hay año años su al lo les"
    ),
    "fr": _vocabulary(
        "les des du une dans pour pas sur aux avec ce cette ces ses son sa ont par plus ne "
        "il elle mais ou où être été sont leur leurs comme aussi entre sous cet très fait "
        "peut tout tous toute même après avant depuis"
    ),
    "de": _vocabulary(
        "der die das und ist von zu den mit sich des auf für nicht ein eine einer eines dem im "
        "als auch an werden aus er sie wie bei oder nach wird sind wurde wurden einem einen "
        "über nur noch kann können dass diese dieser haben hat"
    ),
    "it": _vocabulary(
        "il gli del della dei delle degli è che per con non come più sono anche alla nel nella "
        "nei nelle suo sua suoi sue ma stato stata essere ha hanno questo questa questi queste "
        "sono molto dopo prima tra fra quando dove ogni"
    ),
    "nl": _vocabulary(
        "het een van dat op te zijn met voor niet aan er ook als bij maar om dan nog door naar "
        "uit over wordt werd worden hij zij deze die dit wat wel heeft hebben waar geen "
        "tussen onder naast"
    ),
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# A linha de licença que o mwoffliner põe no fim de cada página, em inglês, seja qual for a língua.
_FOOTER = re.compile(r"This (?:article|page) is issued from.*?(?:media files\.|$)", re.DOTALL)
MAX_WORDS = 3000  # chega para decidir; o resto do documento só gasta tempo
MIN_HITS = 6  # palavras funcionais mínimas do vencedor para o veredito ser firme
MIN_RATIO = 2.0  # e o vencedor tem de ter pelo menos o dobro do segundo
MIN_DENSITY = (
    0.10  # e pelo menos 10% das palavras: páginas checas com menus em inglês não são inglês
)

# Escritas que identificam a língua (a primeira palavra do nome Unicode do carácter → língua).
_SCRIPTS = {
    "HANGUL": "ko",
    "HIRAGANA": "ja",
    "KATAKANA": "ja",
    "CJK": "zh",
    "CYRILLIC": "ru",
    "ARABIC": "ar",
    "HEBREW": "he",
    "GREEK": "el",
    "THAI": "th",
    "DEVANAGARI": "hi",
}
_UKRAINIAN = re.compile("[іїєґІЇЄҐ]")
SCRIPT_SHARE = 0.3  # fração das letras de uma escrita para decidir por ela
KANA_SHARE = 0.05  # o japonês mistura kanji (CJK) com kana: um pouco de kana chega


def detect_text(text: str) -> str | None:
    """A língua do texto se houver provas firmes (escrita própria ou palavras funcionais), senão
    None. Não adivinha com textos curtos: o chamador cai noutro sinal."""
    sample = _FOOTER.sub(" ", text[:20_000])
    script = _script_language(sample)
    if script is not None:
        return script
    words = [w.casefold() for w in _WORD.findall(sample)][:MAX_WORDS]
    if len(words) < 12:
        return None
    seen = Counter(words)
    hits = {
        language: sum(seen[w] for w in vocabulary if w in seen)
        for language, vocabulary in _FUNCTION_WORDS.items()
    }
    ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
    (best, best_hits), (_, second_hits) = ranked[0], ranked[1]
    if best_hits < MIN_HITS or best_hits < MIN_RATIO * max(second_hits, 1):
        return None
    if best_hits < MIN_DENSITY * len(words):
        return None
    return best


def _script_language(text: str) -> str | None:
    counts: Counter[str] = Counter()
    letters = 0
    for char in text:
        if not char.isalpha():
            continue
        letters += 1
        if char.isascii():
            continue
        try:
            first = unicode_name(char).split(" ", 1)[0]
        except ValueError:
            continue
        if first in _SCRIPTS:
            counts[_SCRIPTS[first]] += 1
    if letters < 20:
        return None
    if counts["ja"] >= KANA_SHARE * letters:
        return "ja"
    language, count = counts.most_common(1)[0] if counts else ("", 0)
    if count < SCRIPT_SHARE * letters:
        return None
    if language == "ru" and _UKRAINIAN.search(text):
        return "uk"
    return language


# --- a decisão -----------------------------------------------------------------------------------


def detect(*, html: str | None, path: str, title: str, text: str, zim_language: str | None) -> str:
    """A língua da página em ISO 639-1 (`und` se nada a indica). Ver o texto do módulo."""
    zim = _zim_language(zim_language)
    declared = declared_language(html)
    from_path = path_language(path)
    verdict = detect_text(f"{title}. {text}")

    # Um `lang` igual ao do ZIM é o valor por omissão do site (o wikem diz `en` em tudo): o
    # caminho, quando indica outra língua, passa à frente.
    candidates = [declared, from_path] if declared != zim else [from_path, declared]
    for candidate in candidates:
        if candidate is not None and (verdict is None or verdict == candidate):
            return candidate
    return verdict or zim or UNDETERMINED


def _zim_language(zim_language: str | None) -> str | None:
    """A língua do ZIM; nos multilingues (`eng,fra,…`) não há uma só: None."""
    if not zim_language:
        return None
    parts = [p for p in zim_language.split(",") if p.strip()]
    if len(parts) != 1:
        return None
    # Uma língua só com código de 3 letras (`ceb`) mantém-no: sem locale, mas o relatório diz qual.
    code = parts[0].strip().lower()
    return to_iso1(code) or (code if re.fullmatch(r"[a-z]{3}", code) else None)


def detect_locale(
    *, html: str | None, path: str, title: str, text: str, zim_language: str | None
) -> str:
    """O locale do Wiki.js onde a página se publica (`detect` mais `wiki_locale`). Uma língua sem
    locale devolve-se tal e qual (`ceb`, `und`): quem chama vê que não está em `WIKIJS_LOCALES`."""
    detected = detect(html=html, path=path, title=title, text=text, zim_language=zim_language)
    zim_tag = (zim_language or "").split(",")[0] if "," not in (zim_language or "") else None
    return wiki_locale(detected, [declared_tag(html), zim_tag]) or detected
