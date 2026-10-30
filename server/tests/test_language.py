"""Língua por página (docs/WIKI-IMPORT.md, secção 5): `lang` da página, caminho, detetor, ZIM.

Os casos vêm da amostra de 164 artigos: wikem `/zh` `/it`, gentoo `/ko`, OSM `FR:` `Cs:`
`Zh-hans:`, docs.python `ko/3.12/`. As fixtures são páginas reais cortadas a menos de 10 KB.
"""

from pathlib import Path

import pytest

from ariadne.sources.zim import content_root
from ariadne.wiki import language
from ariadne.wiki.language import (
    declared_language,
    detect,
    detect_text,
    path_language,
    to_iso1,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wiki" / "doctype"

PT = (
    "A Revolução dos Cravos foi um movimento militar que derrubou o regime do Estado Novo em "
    "Portugal, no dia 25 de abril de 1974. Os capitães do exército não queriam mais a guerra "
    "colonial e o povo saiu à rua para apoiar os militares, com cravos nas mãos, durante a tarde."
)
EN = (
    "The Carnation Revolution was a military coup that overthrew the regime of the Estado Novo "
    "in Portugal on the 25th of April 1974. The captains of the army did not want the colonial "
    "war to continue and the people went out into the streets to support the soldiers."
)
ES = (
    "La Revolución de los Claveles fue un golpe militar que derrocó el régimen del Estado Novo "
    "en Portugal el 25 de abril de 1974. Los capitanes del ejército no querían que la guerra "
    "colonial continuara y el pueblo salió a las calles para apoyar a los soldados."
)
FR = (
    "La révolution des Œillets est un coup d'État militaire qui a renversé le régime de l'Estado "
    "Novo au Portugal le 25 avril 1974. Les capitaines de l'armée ne voulaient plus de la guerre "
    "coloniale et le peuple est descendu dans les rues pour soutenir les soldats."
)
DE = (
    "Die Nelkenrevolution war ein Militärputsch, der das Regime des Estado Novo in Portugal am "
    "25. April 1974 stürzte. Die Hauptmänner der Armee wollten den Kolonialkrieg nicht mehr "
    "und das Volk ging auf die Straße, um die Soldaten zu unterstützen."
)
IT = (
    "La rivoluzione dei garofani è stata un colpo di stato militare che ha rovesciato il regime "
    "dell'Estado Novo in Portogallo il 25 aprile 1974. I capitani dell'esercito non volevano più "
    "la guerra coloniale e il popolo è sceso per le strade per sostenere i soldati."
)
NL = (
    "De Anjerrevolutie was een militaire staatsgreep die het regime van de Estado Novo in "
    "Portugal op 25 april 1974 ten val bracht. De kapiteins van het leger wilden de koloniale "
    "oorlog niet meer en het volk ging de straat op om de soldaten te steunen."
)


def fixture(name: str) -> tuple[str, str]:
    html = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
    return html, " ".join(content_root(html).get_text(" ").split())


@pytest.mark.parametrize(
    ("text", "language"),
    [(PT, "pt"), (EN, "en"), (ES, "es"), (FR, "fr"), (DE, "de"), (IT, "it"), (NL, "nl")],
)
def test_detetor_de_palavras_frequentes(text: str, language: str) -> None:
    assert detect_text(text) == language


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("한국어로 쓰인 문서입니다. 이 문서는 파이썬의 표준 라이브러리에 대해 설명합니다.", "ko"),
        (
            "これは日本語の文書です。この文書はパイソンの標準ライブラリについて説明しています。",
            "ja",
        ),
        ("这是一份用中文写的文档。这份文档介绍了圣保罗大学医学院的历史和领导层。", "zh"),
        (
            "Это документ, написанный по-русски. Этот документ описывает стандартную библиотеку.",
            "ru",
        ),
        (
            "Це документ, написаний українською мовою. Цей документ описує стандартну бібліотеку.",
            "uk",
        ),
    ],
)
def test_detetor_por_escrita(text: str, language: str) -> None:
    assert detect_text(text) == language


def test_detetor_nao_adivinha_com_pouco() -> None:
    assert detect_text("Santa Vitória, município brasileiro") is None
    assert detect_text("") is None
    assert detect_text("a" * 500) is None


def test_menus_em_ingles_numa_pagina_checa_nao_a_tornam_inglesa() -> None:
    menus = "More details at tag info. " * 3
    checo = "Jednosměrná ulice Skupina Omezení Použité na těchto prvcích Vhodné kombinace " * 12
    assert detect_text(menus + checo) is None


def test_a_linha_de_licenca_do_zim_nao_conta() -> None:
    page = (
        "tarren Galês Substantivo feminino colina escarpada monte rochoso. "
        "This article is issued from Wiktionary. The text is licensed under Creative Commons - "
        "Attribution - Sharealike. Additional terms may apply for the media files."
    )
    assert detect_text(page) is None
    assert detect(html=None, path="A/tarren", title="tarren", text=page, zim_language="por") == "pt"


@pytest.mark.parametrize(
    ("code", "iso1"),
    [
        ("por", "pt"),
        ("eng", "en"),
        ("EN_US", "en"),
        ("pt-BR", "pt"),
        ("zh-Hans", "zh"),
        ("fra", "fr"),
        ("spa", "es"),
        ("xx", None),
        ("", None),
        (None, None),
        ("eng,fra", None),
    ],
)
def test_normalizacao_para_iso_639_1(code: str | None, iso1: str | None) -> None:
    assert to_iso1(code) == iso1


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("Faculdade_de_Medicina_-_Universidade_de_S%C3%A3o_Paulo/zh", "zh"),  # wikem
        ("Cervical_facet_dislocation/it", "it"),
        ("Eclean/ko", "ko"),  # gentoo
        ("Handbook%3ASPARC/Blocks/ProfileChoice/ja", "ja"),
        ("Basic_guide_to_write_Gentoo_Ebuilds/fr", "fr"),
        ("FR%3AKey%3Aconveying", "fr"),  # OSM
        ("Cs%3AKey%3Aoneway", "cs"),
        ("Zh-hans%3ASimple_Indoor_Tagging", "zh"),
        ("Pt-br:Tag:highway", "pt"),
        ("docs.python.org/ko/3.12/library/base64.html", "ko"),  # docs.python
        ("docs.python.org/pt-br/3.12/library/os.html", "pt"),
        ("docs.python.org/3.12/library/os.html", None),
        ("Tag%3Adesignation%3Dcommon", None),
        ("Key:oneway", None),
        ("Handbook%3ASPARC/Blocks/Disks", None),
        ("Revolução_dos_Cravos", None),
        ("questions/44551/cooking-on-a-hot-humid-day", None),
        ("Foo/IT", None),  # maiúsculas: é o tema, não a língua
        ("Foo/no", None),  # `no` é também uma palavra
        ("", None),
    ],
)
def test_lingua_do_caminho(path: str, language: str | None) -> None:
    assert path_language(path) == language


def test_lingua_declarada() -> None:
    assert declared_language('<html lang="ko" data-content_root="../">') == "ko"
    assert declared_language('<html><body lang="EN_US" style="x">') == "en"
    assert declared_language('<html class="client-nojs" lang="pt-BR" dir="ltr">') == "pt"
    assert declared_language("<html class='x'><body>") is None
    assert declared_language(None) is None


def run(name: str, path: str, zim: str, title: str = "") -> str:
    html, text = fixture(name)
    return detect(html=html, path=path, title=title, text=text, zim_language=zim)


def test_wikem_o_caminho_vence_o_lang_por_omissao() -> None:
    """Todas as páginas do wikem dizem `lang="en"`; as traduções distinguem-se pelo caminho."""
    zh = "Faculdade_de_Medicina_-_Universidade_de_S%C3%A3o_Paulo/zh"
    assert run("wikem_zh", zh, "eng") == "zh"
    assert run("wikem_it", "Cervical_facet_dislocation/it", "eng") == "it"
    assert run("wikem_en", "Nickel_toxicity", "eng") == "en"


def test_gentoo_e_osm_pelo_caminho() -> None:
    assert run("gentoo_ko", "Eclean/ko", "eng") == "ko"
    assert run("gentoo_ja", "Handbook%3ASPARC/Blocks/ProfileChoice/ja", "eng") == "ja"
    assert run("osm_fr", "FR%3AKey%3Aconveying", "eng") == "fr"
    assert run("osm_cs", "Cs%3AKey%3Aoneway", "eng") == "cs"


def test_docs_python_pelo_lang_da_pagina() -> None:
    assert run("docspython_ko", "docs.python.org/ko/3.12/library/base64.html", "eng") == "ko"


def test_osm_zh_hans_com_corpo_em_ingles_fica_em_ingles() -> None:
    """O prefixo diz `Zh-hans`, mas o corpo é inglês com uma linha chinesa: manda o conteúdo."""
    english = EN * 4
    result = detect(
        html=None,
        path="Zh-hans%3ASimple_Indoor_Tagging",
        title="",
        text=english,
        zim_language="eng",
    )
    assert result == "en"


def test_o_zim_e_o_ultimo_recurso() -> None:
    assert run("wiktionary_pt", "A/moiareis", "por") == "pt"
    assert run("wikipedia_pt", "Almagesto", "por") == "pt"
    assert run("recipes", "publicdomainrecipes.com/ketchup/", "eng") == "en"


def test_o_lang_errado_do_ifixit_perde_para_o_texto() -> None:
    """As páginas do ifixit dizem `lang="fr"` mas escrevem em inglês."""
    html, text = fixture("ifixit_en")
    assert declared_language(html) == "fr"
    result = detect(
        html=html,
        path="Device/Samsung_Chromebook_XE500C21-A03US",
        title="",
        text=text,
        zim_language="eng",
    )
    assert result == "en"


def test_a_pagina_diz_pt_num_zim_ingles() -> None:
    """O manual AUTOMAIN do cd3wd: o ZIM é inglês e o `lang` também, mas o texto é português."""
    result = detect(
        html='<body lang="EN_US">',
        path="VITAHTML/SUBLEV/AUTO/AUTOMAIN.HTM",
        title="",
        text=PT * 6,
        zim_language="eng",
    )
    assert result == "pt"


def test_zim_multilingue_sem_sinais_fica_indeterminado() -> None:
    assert (
        detect(
            html=None,
            path="Marian%20Storm.39369",
            title="",
            text="Marian Storm",
            zim_language="eng,fra,deu",
        )
        == "und"
    )
    assert detect(html=None, path="a", title="", text="", zim_language=None) == "und"
    assert detect(html=None, path="a", title="", text=PT, zim_language="eng,fra,deu") == "pt"


def test_zim_em_iso3_converte() -> None:
    assert detect(html=None, path="a", title="", text="curto", zim_language="por") == "pt"
    assert detect(html=None, path="a", title="", text="curto", zim_language="fra") == "fr"


# --- da língua ao locale do Wiki.js (docs/WIKI-IMPORT.md, secção 5.1) -------------------------


def test_catalog_has_the_70_locales_of_wikijs_and_publishes_the_routable_ones() -> None:
    assert len(language.WIKIJS_CATALOG) == 70
    # `sr-latn` não passa no regex de encaminhamento do Wiki.js (`^[A-Z]{2}(-[A-Z]{2})?$`)
    assert {"sr-latn"} == language.WIKIJS_CATALOG - language.WIKIJS_LOCALES


@pytest.mark.parametrize(
    ("code", "locale"),
    [
        ("pt", "pt"), ("por", "pt"), ("fra", "fr"), ("fre", "fr"), ("deu", "de"), ("swa", "sw"),
        ("is", "is-is"), ("isl", "is-is"), ("hin", "hi-in"), ("eus", "eu-es"), ("mkd", "mk-mk"),
        ("no", "nb"), ("nor", "nb"), ("nob", "nb"), ("nno", "nn"), ("zho", "zh"), ("srp", "sr"),
    ],
)  # fmt: skip
def test_language_codes_map_to_the_wikijs_locale(code: str, locale: str) -> None:
    assert language.wiki_locale(code) == (locale if locale in language.WIKIJS_LOCALES else None)


@pytest.mark.parametrize("code", ["ceb", "war", "arz", "mlg", "cym", "hbs", "und", "eng,fra", ""])
def test_languages_without_a_locale_are_not_published(code: str) -> None:
    assert language.wiki_locale(code) is None


@pytest.mark.parametrize(
    ("language_code", "tags", "locale"),
    [
        ("pt", ["pt-BR"], "pt-br"),
        ("pt", ["pt-PT"], "pt"),
        ("pt", [None, "por"], "pt"),
        ("zh", ["zh-Hant"], "zh-tw"),
        ("zh", ["zh-TW"], "zh-tw"),
        ("zh", ["zh-Hans"], "zh"),
        ("zh", ["zh-CN"], "zh"),
        ("sr", ["sr-Latn"], "sr"),  # `sr-latn` não encaminha: junta-se ao `sr`
        ("en", ["en-GB"], "en"),
        ("pt", ["en-US"], "pt"),  # a etiqueta de outra língua não decide a variante
    ],
)
def test_variants_fold_into_the_base_language_except_pt_br_and_traditional_chinese(
    language_code: str, tags: list[str | None], locale: str
) -> None:
    assert language.wiki_locale(language_code, tags) == locale


def test_detect_locale_uses_the_declared_variant_and_returns_unmapped_languages_as_they_are() -> (
    None
):
    page = '<html lang="zh-Hant"><body>x</body></html>'
    assert (
        language.detect_locale(html=page, path="a", title="", text="", zim_language="zho")
        == "zh-tw"
    )
    assert (
        language.detect_locale(html=None, path="Wikipedia/x", title="", text="", zim_language="ceb")
        == "ceb"
    )
    assert (
        language.detect_locale(html=None, path="x", title="", text="", zim_language="eng,fra,deu")
        == "und"
    )


def test_three_letter_codes_convert_and_those_without_a_two_letter_form_do_not() -> None:
    assert language.to_iso1("swa") == "sw"
    assert language.to_iso1("ceb") is None
    assert language.to_iso1("pt-BR") == "pt"
    assert language.to_iso1("sw") == "sw"  # antes só os códigos de `_CODES` passavam
