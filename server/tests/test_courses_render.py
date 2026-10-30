"""O saneamento do HTML das lições: nada de ativo chega ao Moodle (sem limpeza) nem ao leitor."""

import re

import pytest

from ariadne.courses.render import code_html, markdown_to_html, sanitize_html

VECTORS = [
    "<!--><script>alert(1)</script>-->",
    "<!-- --><script>alert(1)</script>",
    "<![CDATA[><script>alert(1)</script>]]>",
    "<![CDATA[x]]><script>alert(1)</script>",
    "<?php echo 1; ?><script>alert(1)</script>",
    "<!DOCTYPE html><script>alert(1)</script>",
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil/x.js></SCRIPT>",
    "<img src=x onerror=alert(1)>",
    '<img src="x" onerror="alert(1)" alt="a">',
    "<svg onload=alert(1)>",
    "<svg><script>alert(1)</script></svg>",
    "<math><mtext><script>alert(1)</script></mtext></math>",
    "<iframe src=javascript:alert(1)></iframe>",
    "<iframe srcdoc='<script>alert(1)</script>'></iframe>",
    "<a href=javascript:alert(1)>x</a>",
    "<a href=' JaVaScRiPt:alert(1)'>x</a>",
    "<a href='java\tscript:alert(1)'>x</a>",
    "<a href='jav&#x09;ascript:alert(1)'>x</a>",
    "<a href='&#106;avascript:alert(1)'>x</a>",
    "<a href='data:text/html,<script>alert(1)</script>'>x</a>",
    "<a href=vbscript:msgbox(1)>x</a>",
    "<p onclick=alert(1) style='x:expression(alert(1))'>x</p>",
    "<div style=background:url(javascript:alert(1))>x</div>",
    "<form action=javascript:alert(1)><input type=submit></form>",
    "<button formaction=javascript:alert(1)>x</button>",
    "<object data=javascript:alert(1)></object>",
    "<embed src=javascript:alert(1)>",
    "<base href=javascript:alert(1)//>",
    "<link rel=stylesheet href=//evil/x.css>",
    "<meta http-equiv=refresh content='0;url=javascript:alert(1)'>",
    "<noscript><p title='</noscript><img src=x onerror=alert(1)>'>",
    "<style>@import 'javascript:alert(1)'</style>",
    "<textarea><script>alert(1)</script></textarea>",
    "<template><script>alert(1)</script></template>",
    "<details open ontoggle=alert(1)>x</details>",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "&#60;script&#62;alert(1)&#60;/script&#62;",
    "<b <script>alert(1)</script>>x</b>",
    "<<script>script>alert(1)<</script>/script>",
    "<a href=\"x\" href='javascript:alert(1)'>x</a>",
]
DANGEROUS = re.compile(
    r"<script|<svg|<iframe|<img|<style|<object|<embed|<form|<input|<link|<meta|<base|<math|"
    r"<template|<textarea|<!--|<!\[|<\?|<!doctype|\son\w+\s*=|javascript:|vbscript:|data:text|"
    r"\sstyle\s*=|\ssrc\s*=|\ssrcdoc\s*=",
    re.IGNORECASE,
)


@pytest.mark.parametrize("vector", VECTORS)
def test_nada_ativo_sobrevive(vector: str) -> None:
    for html in (
        sanitize_html(vector),
        markdown_to_html(vector),
        markdown_to_html(f"texto\n\n{vector}\n"),
    ):
        assert not DANGEROUS.search(html), (vector, html)


def test_vetores_de_comentario_e_cdata_nao_deixam_passar_o_script() -> None:
    assert sanitize_html("<!--><script>alert(1)</script>-->") == ""
    assert "script" not in sanitize_html("<![CDATA[><script>alert(1)</script>]]>")
    assert sanitize_html("a<!-- escondido -->b") == "ab"


def test_entidades_ficam_texto() -> None:
    assert (
        sanitize_html("&lt;script&gt;alert(1)&lt;/script&gt;")
        == "&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    assert sanitize_html("1 &lt; 2 &amp; 3") == "1 &lt; 2 &amp; 3"


def test_o_que_e_seguro_mantem_se() -> None:
    html = markdown_to_html(
        "**forte** e `código`\n\n- item\n\n"
        "[site](https://exemplo.org/a?b=1&c=2) e [e](mailto:a@example.org)\n\n"
        "```js\nconst a = 1 < 2;\n```\n\n<table><tr><th colspan=2>x</th></tr></table>"
    )
    assert "<strong>forte</strong>" in html and "<code>código</code>" in html
    assert '<a href="https://exemplo.org/a?b=1&amp;c=2">site</a>' in html
    assert '<a href="mailto:a@example.org">e</a>' in html
    assert '<code class="language-js">const a = 1 &lt; 2;' in html
    assert '<th colspan="2">x</th>' in html


def test_atributos_so_os_permitidos_e_escapados() -> None:
    html = sanitize_html('<a href="https://x.org" title=\'a"b\' target=_blank onclick=x>l</a>')
    assert html == '<a href="https://x.org" title="a&quot;b">l</a>'
    assert sanitize_html('<code class="language-js x">a</code>') == "<code>a</code>"
    assert sanitize_html('<td colspan="2<script>">a</td>') == "<td>a</td>"


def test_imagem_vira_texto_alternativo_escapado() -> None:
    assert sanitize_html('<img src=x alt="<b>">') == "[imagem: &lt;b&gt;]"
    assert sanitize_html("<img src=x>") == ""


def test_etiquetas_desconhecidas_ficam_so_com_o_texto() -> None:
    assert sanitize_html("<blink>olá <b>mundo</b></blink>") == "olá <b>mundo</b>"


def test_codigo_e_escapado() -> None:
    html = code_html("<script>alert(1)</script>", 'js"><script>')
    assert "<script>" not in html
    assert not DANGEROUS.search(html.replace("<pre>", "").replace("<code", ""))
