# ruff: noqa: E501  (textos reais da amostra, em linhas compridas)
"""A curadoria de receitas (piloto do based.cooking, 28/10/2026): exemplos reais da amostra em que
o curador apagava a linha de tempos, repetia o título, e o pedido de donativos fazia o verificador
reter a página. Sem base de dados."""

from ariadne.curation import fidelity
from ariadne.curation.curator import FixedBlock, attach_fixed, parse_reply, split_fixed
from ariadne.wiki import convert

TOLERANCE = 0.1

CHALLAH = """A kind of braided bread preparation.

- ⏲️ Prep time: 20 min
- 🍳 Cook time: 40 min
- 🍽️ Servings: 2-4

NOTE: Prep time does not account for the 3-4 hours the dough must be left to rise and cool (see directions 5, 10, &amp; 12).

## Ingredients

- 600ml ( 1 Pint ) warm water
- 1 tablespoon dry active baking yeast
- 7 tablespoons honey
- 1.5kg flour
- 3 eggs

## Directions

1. Fill a large bowl with the water, and sprinkle with the yeast.
2. Whisk together: honey, oil, 2 eggs, and salt. Add to bowl.
3. Add flour to bowl in small amounts, beating/whisking after each addition."""

# O que o Gemma 3 12B devolveu: resumo vago no lugar dos tempos e a nota apagada.
CHALLAH_GEMMA = """This article provides a recipe for challah bread, a braided bread preparation. It details the ingredients, preparation steps, and cooking instructions, including timings and serving suggestions.

## Ingredients
- 600ml ( 1 Pint ) warm water
- 1 tablespoon dry active baking yeast
- 7 tablespoons honey
- 1.5kg flour
- 3 eggs

## Directions
1. Fill a large bowl with the water, and sprinkle with the yeast.
2. Whisk together: honey, oil, 2 eggs, and salt. Add to bowl.
3. Add flour to bowl in small amounts, beating/whisking after each addition."""

GRANOLA = """- ⏲️ Prep time: 10 min
- 🍳 Cook time: 40 min
- 🍽️ Servings: 5

## Ingredients

- 3 cups Oats
- 1/2 cup Olive Oil
- 1 cup Chopped Nuts (Almonds, Pumpkin Seeds, etc)

## Directions

1. Preheat oven to 315° F
2. In a large bowl, mix: Oats, Oil, Maple Syrup, Chopped Nuts and Spices
7. Bake 10 minutes, take out of oven, **do not stir** (helps form clumps), let it cool"""

# Um modelo pequeno devolve o texto igual e acrescenta o título como «# Granola».
GRANOLA_COM_TITULO = "# Granola\n\n" + GRANOLA.replace("\n\n## ", "\n## ")


def test_o_curador_que_apaga_a_linha_de_tempos_reprova() -> None:
    report = fidelity.check(CHALLAH, CHALLAH_GEMMA, TOLERANCE)

    assert not report.ok
    assert any("lost" in p for p in report.problems)


def test_o_bloco_fixo_sai_do_que_o_curador_recebe_e_volta_inteiro() -> None:
    rest, fixed = split_fixed(CHALLAH)

    assert "Prep time" not in rest and "NOTE" not in rest
    assert rest.startswith("A kind of braided bread preparation.\n\n## Ingredients")
    assert fixed.head.splitlines()[0] == "- ⏲️ Prep time: 20 min"
    assert "NOTE: Prep time does not account for the 3-4 hours" in fixed.head
    assert not fixed.tail
    back = attach_fixed(CHALLAH_GEMMA, fixed)
    assert back.index("Prep time: 20 min") < back.index("## Ingredients")
    assert fidelity.check(CHALLAH, back, TOLERANCE).ok


def test_sem_metadados_nem_atribuicao_o_texto_fica_como_esta() -> None:
    text = "Intro.\n\n## Ingredients\n\n- 2 Eggs\n\n## Directions\n\n1. Cook."

    assert split_fixed(text) == (text, FixedBlock())
    assert attach_fixed("Texto.", FixedBlock()) == "Texto."


def test_um_so_item_de_metadados_tambem_e_fixo() -> None:
    # Ceviche e butter biscuits: «Prep time» sozinho ou «Wait time» perdiam-se no resumo.
    rest, fixed = split_fixed("Intro.\n\n- ⏲️ Prep time: 10 min\n\n## Ingredients\n\n- Fish")

    assert fixed.head == "- ⏲️ Prep time: 10 min" and "Prep" not in rest
    _, wait = split_fixed("- ⏲️ Prep time: 10 min\n- ⏲️ Wait time: 1h\n- 🍳 Cook time: 15 min")
    assert wait.head.count("\n") == 2


def test_a_atribuicao_fica_fora_do_llm_e_volta_no_fim() -> None:
    text = "Intro.\n\n## Directions\n\n1. Cook.\n\n## Contributor(s)\n\n- [Luke Smith](https://lukesmith.xyz)"

    rest, fixed = split_fixed(text)

    assert "Luke Smith" not in rest and rest.endswith("1. Cook.")
    back = attach_fixed("Resumo.\n\n## Directions\n1. Cook.", fixed)
    assert back.rstrip().endswith("- [Luke Smith](https://lukesmith.xyz)")
    assert fidelity.check(text, back, TOLERANCE).ok


def test_bloco_fixo_no_fim_quando_o_curador_nao_poe_titulos() -> None:
    assert (
        attach_fixed("Resumo.", FixedBlock("- Prep time: 5 min"))
        == "Resumo.\n\n- Prep time: 5 min\n"
    )


def test_o_titulo_repetido_pelo_curador_nao_e_um_nome_acrescentado() -> None:
    assert fidelity.check(GRANOLA, GRANOLA_COM_TITULO, TOLERANCE).ok  # «# Granola» abre o texto
    resumo = "A tasty Granola recipe.\n\n" + GRANOLA
    assert not fidelity.check(GRANOLA, resumo, TOLERANCE).ok  # sem o título, é um nome novo
    assert fidelity.check(GRANOLA, resumo, TOLERANCE, context="Granola").ok


def test_o_contexto_nao_esconde_nomes_inventados() -> None:
    inventado = GRANOLA + "\n\nServed at Harrods."

    assert not fidelity.check(GRANOLA, inventado, TOLERANCE, context="Granola").ok


def test_numeros_perdidos_continuam_a_reprovar() -> None:
    curado = GRANOLA.replace("40 min", "").replace("315° F", "hot")

    report = fidelity.check(GRANOLA, curado, TOLERANCE, context="Granola")

    assert not report.ok and any("lost" in p for p in report.problems)


def test_min_e_minutes_sao_a_mesma_quantidade() -> None:
    a = "Bake for 15 min, then rest 2 hrs."
    b = "Bake for 15 minutes, then rest 2 hours."

    assert fidelity.check(a, b, TOLERANCE).ok
    assert fidelity.extract(a).quantities == fidelity.extract(b).quantities
    assert not fidelity.check(a, "Bake for 25 minutes, then rest 2 hours.", TOLERANCE).ok


def test_may_verbo_nao_e_o_mes() -> None:
    assert fidelity.check("Add salt.", "You may add salt.", TOLERANCE).ok
    assert "may" in fidelity.extract("Born on May 5 in Lisbon.").months
    assert "may" not in fidelity.extract("You may add salt.").months
    assert not fidelity.check("Born in Lisbon.", "Born on May 5 in Lisbon.", TOLERANCE).ok


COLCANNON_HTML = """<html><body><h1>Colcannon Bake</h1>
<p>Potatoes and cabbage.</p>
<h2>Directions</h2><ol><li>Bake in the preheated oven for 40 minutes.</li></ol>
<h2>Contributor(s)</h2>
<ul><li><a href="https://github.com/joelmaxuel">Joel Maxuel</a>
<ul>
<li><img alt="XMR Logo" src="xmr.svg">Monero: <code>4AdhfxUfFbPd11epEKsuDqBm4jJEapK9UUhgFH8ss7aSAhtox1Li4B6LZwoEuZ6W8BJUCpRynUXFCWcDUzKUd7Cr99kk4tM</code></li>
<li><img alt="BTC Logo" src="btc.svg">Bitcoin: <code>bc1qvpj4juacefyl6v7f6f7helggsmmxn97s7h6yzd</code></li>
<li><a href="https://rdwilson.xyz/donate.html">Donate/Tip</a></li>
</ul></li></ul></body></html>"""


def markdown_of(html: str) -> str:
    root, _ = convert.prepare(html)
    return convert.to_markdown(root, {}, {})


def test_o_pedido_de_donativos_sai_e_o_colaborador_fica() -> None:
    markdown = markdown_of(COLCANNON_HTML)

    for lixo in ("Monero", "Bitcoin", "bc1q", "XMR", "BTC", "Donate"):
        assert lixo not in markdown
    assert "Joel Maxuel" in markdown and "## Contributor(s)" in markdown
    assert "Bake in the preheated oven for 40 minutes" in markdown


def test_receita_sem_donativos_nao_perde_nada() -> None:
    html = "<html><body><p>Mix 3 Eggs with Bitcoin-sized chunks of butter.</p><ul><li>Tip: salt.</li></ul></body></html>"

    markdown = markdown_of(html)

    assert "Bitcoin-sized" in markdown and "Tip: salt." in markdown


def test_resposta_com_quebras_de_linha_reais_na_string_e_aceite() -> None:
    # Um modelo de 7B (Kaggle) devolvia isto em 25 de 54 páginas e o parse estrito recusava-o.
    reply = '{"verdict": "accept", "reason": "ok", "issues": [], "markdown": "Resumo.\n\n## Ingredients\n\n- eggs\t(2)"}'

    curated = parse_reply(reply)

    assert curated.verdict == "accept" and "## Ingredients" in curated.markdown
    assert "- eggs\t(2)" in curated.markdown


def test_nome_que_so_perde_a_maiuscula_nao_esta_perdido() -> None:
    original = "Mix the flour with Olive Oil and Maple Syrup, then bake."
    lower = "Mix the flour with olive oil and maple syrup, then bake."

    assert fidelity.check(original, lower, TOLERANCE).ok
    # apagar a palavra de vez continua a reprovar
    assert not fidelity.check(
        original, "Mix the flour with oil and syrup, then bake.", TOLERANCE
    ).ok
