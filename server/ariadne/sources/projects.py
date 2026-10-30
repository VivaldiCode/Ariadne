"""O projeto Wikimedia e o `site` do Wikidata de uma fonte (ARCHITECTURE.md, secção 8).

Do nome do ZIM (`wikipedia_pt_all`, `wikiquote_en_maxi`) tira-se o projeto (`wikipedia`) e a
língua (`pt`); com eles, o identificador de site que o Wikidata usa nos *sitelinks* (`ptwiki`,
`enwikiquote`). Uma fonte que não é de um projeto conhecido não tem `project` nem `site`.
"""

import re

# projeto -> sufixo do identificador de site no Wikidata (`<língua><sufixo>`).
SITE_SUFFIX = {
    "wikipedia": "wiki",
    "wikibooks": "wikibooks",
    "wikiquote": "wikiquote",
    "wikisource": "wikisource",
    "wikinews": "wikinews",
    "wikiversity": "wikiversity",
    "wikivoyage": "wikivoyage",
    "wiktionary": "wiktionary",
}
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,8})?$")
# Palavras do nome do ZIM que parecem códigos de língua e não o são.
_NOT_LANGUAGES = frozenset({"all", "top", "mini", "maxi", "nopic", "novid", "app", "pic"})


# Códigos de língua com mais de um pedaço no nome do ZIM (`wikipedia_zh-classical_…`):
# (pedaços) -> (código nas ligações de língua, língua do site no Wikidata).
COMPOUND = {
    ("zh", "min", "nan"): ("zh-min-nan", "zh_min_nan"),
    ("zh", "classical"): ("zh-classical", "zh_classical"),
    ("be", "tarask"): ("be-x-old", "be_x_old"),
    ("bat", "smg"): ("bat-smg", "bat_smg"),
    ("en", "simple"): ("simple", "simple"),  # a Simple English tem site próprio: `simplewiki`
}


def identify(zim_name: str | None, language: str | None) -> dict[str, str]:
    """`{project, wiki_language, site}` do nome do ZIM (e da língua da fonte, se o nome não a
    diz); vazio se não é um projeto Wikimedia conhecido ou se a língua não é fiável."""
    tokens = (zim_name or "").casefold().replace("-", "_").split("_")
    project = tokens[0] if tokens else ""
    if project not in SITE_SUFFIX:
        return {}
    rest = tokens[1:]
    for pieces, (code, site_language) in sorted(COMPOUND.items(), key=lambda kv: -len(kv[0])):
        if tuple(rest[: len(pieces)]) == pieces:
            return {
                "project": project,
                "wiki_language": code,
                "site": site_language + SITE_SUFFIX[project],
            }
    named = rest[0] if rest else ""
    # «classical» (e afins) sem ser o chinês clássico: não se sabe de que site são. Fora.
    if "classical" in rest[1:2]:
        return {}
    wiki_language = named if _LANGUAGE.match(named) and named not in _NOT_LANGUAGES else ""
    wiki_language = wiki_language or (language or "").casefold().split("-")[0]
    if not _LANGUAGE.match(wiki_language):
        return {}
    site = wiki_language.replace("-", "_") + SITE_SUFFIX[project]
    return {"project": project, "wiki_language": wiki_language, "site": site}
