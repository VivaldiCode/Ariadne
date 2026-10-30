"""A regra histórica e a língua da pergunta: classificadores simples, sem base de dados."""

import pytest

from ariadne.router_rules import classify, detect_language, fold


@pytest.mark.parametrize(
    "query",
    [
        "Revolução dos Cravos",
        "Carnation Revolution",
        "Quando foi a Revolución francesa",
        "causes of the French Revolution",
        "La bataille de Waterloo",
        "Tratado de Tordesilhas",
        "Treaty of Versailles terms",
        "Primeira Guerra Mundial",
        "Why did World War 2 start?",
        "reinado de D. Manuel I",
        "golpe de estado no Chile",
        "independência do Brasil",
        "o que aconteceu em 1755",
        "Lisbon earthquake in 1755",
        "Revolução dos Cravos de 1974",
        "o que se passou a 25 de abril de 1974",
        "Cold War causes",
        "history of Portugal during the Roman Empire",
        "what happened in 44 BC",
        "ano 476 queda de Roma",
        "Roman Empire",
        "século XV descobrimentos",
        "A Idade Média na Europa",
        "the Middle Ages",
    ],
)
def test_perguntas_historicas(query: str) -> None:
    result = classify(query)
    assert result.historical, query
    assert result.reasons  # explica porquê


@pytest.mark.parametrize(
    "query",
    [
        "how to install postgres",
        "bacalhau à brás receita",
        "SQL window functions",
        "browser history clear cache",
        "error 404 not found",
        "resolution 1080p screen",
        "python list comprehension",
        "quanto custa 100% algodão",
        "melhores restaurantes em Lisboa",
        "what is a vector database",
        "chocolate cake recipe",
        "serve on port 8080",
        "GTX 1080 drivers",
        "error 1054 mysql",
        "windows 2019 server install",
        "independent variables regression",
        "tight coupling in python",
        "Star Wars cast",
        "React Empire app",
        "browser history of Chrome",
        "25 de abril promoções",
        "April 25 sales report",
        "history of Python",
        "ant colony optimization",
    ],
)
def test_perguntas_nao_historicas(query: str) -> None:
    result = classify(query)
    assert not result.historical, (query, result.reasons)
    assert result.reasons == ()


def test_razoes_dizem_o_que_apanhou() -> None:
    result = classify("A Revolução dos Cravos de 25 de abril de 1974")

    assert "keyword:revolucao" in result.reasons
    assert "year:1974" in result.reasons
    assert "date:25 de abril" in result.reasons
    assert classify("what happened in 44 BC").reasons == ("year:44 bc",)


def test_fold() -> None:
    assert fold("Revolução É Œuvre") == "revolucao e œuvre"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Quando foi a Revolução dos Cravos?", "pt"),
        ("Revolução dos Cravos", "pt"),
        ("When was the Carnation Revolution?", "en"),
        ("Carnation Revolution", None),  # ASCII sem palavras funcionais: não se adivinha
        ("Lisboa", None),
        ("gato", None),
        ("Mercurio", None),
        ("¿Cuándo fue la revolución de los claveles?", "es"),
        ("revolucion de los claveles", "es"),
        ("Quelle est la capitale de la France ?", "fr"),
        ("Wer war der erste Kaiser von Deutschland?", "de"),
        ("Chi era il primo imperatore di Roma?", "it"),
        ("日本の首都", None),
    ],
)
def test_detect_language(text: str, expected: str | None) -> None:
    assert detect_language(text) == expected


def test_detect_language_pode_adivinhar_se_o_chamador_quiser() -> None:
    assert detect_language("Carnation Revolution", ascii_default="en") == "en"
    assert detect_language("capital of Portugal") == "en"
