"""A obra de um livro entre línguas: chaves, ranhuras e a regra de entrada (ARIAD-32)."""

import pytest

from ariadne.books.works import (
    ORIGINAL,
    TRANSLATION,
    UNKNOWN,
    Holder,
    Work,
    WorkRegistry,
    author_signature,
    language_code,
    title_signature,
)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Os Maias", "OS MAIAS"),
        ("Moby Dick; Or, The Whale", "Moby-Dick"),
        ("Frankenstein; Or, The Modern Prometheus", "Frankenstein"),
        ("The Count of Monte Cristo", "Count of Monte Cristo"),
        ("Le Comte de Monte-Cristo", "Comte de Monte Cristo"),
        ("Memórias Póstumas de Brás Cubas", "Memorias postumas de Bras Cubas"),
    ],
)
def test_titulos_da_mesma_edicao_dao_a_mesma_assinatura(a: str, b: str) -> None:
    assert title_signature(a) == title_signature(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Guerra ou Paz", "Guerra"),  # o «ou» solto faz parte do título
        ("Poemas, Tomo I", "Poemas, Tomo II"),  # volumes diferentes
        ("Os Lusíadas", "The Lusiads"),  # só o Wikidata liga traduções
    ],
)
def test_titulos_diferentes_nao_se_juntam(a: str, b: str) -> None:
    assert title_signature(a) != title_signature(b)


def test_autor_sem_acentos_datas_nem_ordem() -> None:
    assert author_signature(["Verne, Jules, 1828-1905"]) == author_signature(["Jules Verne"])
    assert author_signature(["Queirós, Eça de"]) == author_signature(["Eça de Queirós (1845-1900)"])
    assert author_signature([]) == "" and author_signature(["Anonymous"]) == ""
    assert author_signature(["Various"]) == "" and author_signature(["Anónimo"]) == ""


def test_codigos_de_lingua_do_livro() -> None:
    assert language_code("pt-BR") == "pt" and language_code("por") == "pt"
    assert language_code("ceb") == "ceb"  # sem código de 2 letras: fica o de 3
    assert language_code("eng,fra") is None and language_code("") is None


def test_regra_das_ranhuras() -> None:
    registry = WorkRegistry()
    original = Work("wd:Q1", "wikidata", "pt", ORIGINAL)
    translation = Work("wd:Q1", "wikidata", "pt", TRANSLATION)
    unknown = Work("wd:Q1", "wikidata", "pt", UNKNOWN)

    assert registry.decide(None).kind == "admit"
    assert registry.decide(translation).kind == "admit"  # ranhura livre
    registry.record(translation, "tr-1")

    assert registry.decide(translation).kind == "refuse"  # outra tradução na mesma língua
    assert registry.decide(unknown).kind == "refuse"
    assert registry.decide(Work("wd:Q1", "wikidata", "en", TRANSLATION)).kind == "admit"
    decision = registry.decide(original)
    assert decision.kind == "supersede" and decision.holder == Holder("tr-1", TRANSLATION)

    registry.record(original, "orig-1")
    assert registry.decide(original).kind == "refuse"  # nunca duas edições do original
    assert registry.decide(translation).kind == "refuse"


@pytest.mark.parametrize(
    "title", ["Война и мир. Том 1", "Анна Каренина. Том 1", "戦争と平和", "1984"]
)
def test_titulos_sem_letras_latinas_nao_identificam_a_obra(title: str) -> None:
    assert title_signature(title) == ""
