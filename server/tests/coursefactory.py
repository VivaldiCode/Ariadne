"""Cursos de teste: um curso canónico pequeno e um ZIM sintético com a estrutura do freeCodeCamp."""

import json
from pathlib import Path

from libzim.writer import Creator, Hint, Item, StringProvider

from ariadne.courses.schema import (
    CodeExampleBlock,
    Course,
    ExerciseBlock,
    ExerciseTest,
    Lesson,
    License,
    Module,
    Origin,
    QuizBlock,
    QuizChoice,
    QuizQuestion,
    SolutionBlock,
    TextBlock,
    VideoBlock,
    WorkedExampleBlock,
)
from ariadne.sources.license import LicenseClass

SOMA_STARTER = "function soma(a, b) {\n  // escreve aqui\n  return 0;\n}\n"
SOMA_SOLUTION = "function soma(a, b) {\n  return a + b;\n}\n"


def exercise_lesson() -> Lesson:
    """Uma lição com texto, exemplo, exercício corrigível (3 testes) e solução."""
    return Lesson(
        id="somar",
        title="Somar dois números",
        summary="Escreve uma função que soma dois números.",
        objectives=["Escrever uma função com dois parâmetros"],
        blocks=[
            TextBlock(
                markdown="Escreve a função `soma(a, b)` que devolve a soma de **a** e **b**."
            ),
            CodeExampleBlock(language="javascript", code="console.log(1 + 2);\n", caption="Soma"),
            ExerciseBlock(
                id="somar-ex",
                title="Somar dois números",
                language="javascript",
                starter_code=SOMA_STARTER,
                tests=[
                    ExerciseTest(
                        id="t1", description="soma é uma função", code="assert.isFunction(soma);"
                    ),
                    ExerciseTest(
                        id="t2",
                        description="soma(1, 2) devolve 3",
                        code="assert.strictEqual(soma(1, 2), 3);",
                    ),
                    ExerciseTest(
                        id="t3",
                        description="soma(-1, 1) devolve 0",
                        code="assert.strictEqual(soma(-1, 1), 0);",
                    ),
                ],
            ),
            SolutionBlock(exercise="somar-ex", language="javascript", code=SOMA_SOLUTION),
        ],
    )


def text_lesson(lesson_id: str = "variaveis", requires: list[str] | None = None) -> Lesson:
    return Lesson(
        id=lesson_id,
        title="Variáveis e valores",
        summary="O que é uma variável.",
        requires=requires or [],
        blocks=[
            TextBlock(markdown="Uma variável guarda um valor.\n\n```js\nlet x = 1;\n```"),
            WorkedExampleBlock(
                statement="Troca dois valores.", language="javascript", code="[a, b] = [b, a];"
            ),
            QuizBlock(
                id="quiz-variaveis",
                questions=[
                    QuizQuestion(
                        prompt="Que palavra declara uma constante?",
                        choices=[QuizChoice(text="const", correct=True), QuizChoice(text="var")],
                    )
                ],
            ),
            VideoBlock(title="Aula", url="https://exemplo.org/v", transcript="Texto da aula."),
        ],
    )


def make_course(
    course_id: str = "curso-teste",
    version: str = "2026-09",
    *,
    requires: list[str] | None = None,
    title: str = "Curso de teste",
    extra_lessons: bool = False,
) -> Course:
    """Dois módulos: «Bases» (variáveis → somar) e «Funções» (somar → repetir, opcional)."""
    somar = exercise_lesson().model_copy(update={"requires": ["bases/variaveis"]})
    modules = [
        Module(
            id="bases",
            title="Bases",
            summary="Os primeiros passos.",
            lessons=[text_lesson(), somar],
        )
    ]
    if extra_lessons:
        modules.append(
            Module(
                id="funcoes",
                title="Funções",
                lessons=[
                    Lesson(
                        id="repetir",
                        title="Repetir um texto",
                        requires=["bases/somar"],
                        blocks=[TextBlock(markdown="Repete um texto n vezes com `repeat`.")],
                    )
                ],
            )
        )
    return Course(
        id=course_id,
        title=title,
        version=version,
        summary="Um curso pequeno para os testes.",
        language="pt",
        programming_language="javascript",
        objectives=["Escrever funções simples"],
        requires=requires or [],
        origin=Origin(source_id="zim:teste", source_version=version),
        license=License(
            name="CC BY-SA 4.0", attribution="Testes do Ariadne", license_class=LicenseClass.OPEN
        ),
        modules=modules,
    )


# --- ZIM sintético com a estrutura do freeCodeCamp ----------------------------------------------

CHALLENGES: dict[str, str] = {
    "basicos/aaa111": """---
id: aaa111
title: Soma de dois números
challengeType: 1
forumTopicId: 1
dashedName: soma-de-dois-numeros
---

# --description--

Escreve uma função <code>soma</code> que devolve a soma de dois números.

# --instructions--

Usa o operador `+`.

# --hints--

`soma` deve ser uma função.

```js
assert.isFunction(soma);
```

`soma(1, 2)` deve devolver 3.

```js
assert.strictEqual(soma(1, 2), 3);
```

O código não deve usar `eval`.

```js
const semComentarios = __helpers.removeJSComments(code);
assert.notMatch(semComentarios, /eval/);
```

# --seed--

## --seed-contents--

```js
function soma(a, b) {
  return 0;
}
```

# --solutions--

```js
function soma(a, b) {
  return a + b;
}
```
""",
    "basicos/bbb222": """---
id: bbb222
title: Inverter um texto
challengeType: 1
dashedName: inverter-um-texto
---

# --description--

Inverte uma string.

# --hints--

`inverter("abc")` deve devolver `"cba"`.

```js
assert.strictEqual(inverter("abc"), "cba");
```

# --seed--

## --seed-contents--

```js
function inverter(texto) {
  return texto;
}
```

# --before-each--

```js
globalThis.chamadas = 0;
```

# --solutions--

```js
// solution required
```
""",
    "basicos/ccc333": """---
id: ccc333
title: Página HTML
challengeType: 0
dashedName: pagina-html
---

# --description--

Cria um título.

# --hints--

A página deve ter um `h1`.

```js
assert.isNotNull(document.querySelector('h1'));
```

# --seed--

## --seed-contents--

```html
<body>
</body>
```

# --solutions--

```html
<body><h1>Olá</h1></body>
```
""",
    "avancado/ddd444": """---
id: ddd444
title: Teste com expect
challengeType: 1
dashedName: teste-com-expect
---

# --description--

Um desafio cujos testes usam outra biblioteca.

# --hints--

Usa expect.

```js
expect(dobro(2)).to.equal(4);
```

Usa um método fora do subconjunto.

```js
assert.fooBar(dobro(2), 4);
```

Este é bom.

```js
assert.equal(dobro(2), 4);
```

# --seed--

## --seed-contents--

```js
// um comentário com um título falso
// # --hints--
function dobro(n) { return n; }
```

# --solutions--

```js
function dobro(n) { return n * 2; }
```
""",
}

INTRO = {
    "testcurso": {
        "title": "Curso sintético",
        "intro": ["Um curso <b>pequeno</b> para os testes.", "Segundo parágrafo."],
        "blocks": {
            "basicos": {"title": "Básicos", "intro": ["Os primeiros desafios."]},
            "avancado": {"title": "Avançado", "intro": ["Mais difícil."]},
        },
    }
}
INDEX = {
    "testcurso": {
        "basicos": [
            {"title": "Soma de dois números", "slug": "aaa111"},
            {"title": "Inverter um texto", "slug": "bbb222"},
            {"title": "Página HTML", "slug": "ccc333"},
        ],
        "avancado": [{"title": "Teste com expect", "slug": "ddd444"}],
    }
}


class _Entry(Item):
    def __init__(self, path: str, content: str, mimetype: str, front: bool = False) -> None:
        super().__init__()
        self._path, self._content, self._mimetype, self._front = path, content, mimetype, front

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._path

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._front}


def make_fcc_zim(
    path: Path,
    *,
    name: str = "freecodecamp_en_testcurso",
    date: str = "2026-08-11",
    challenges: dict[str, str] | None = None,
) -> Path:
    """Um ZIM com o que o `fcc2zim` põe: `index.json`, `intro.json`, um `.md` por desafio e as
    páginas-casca que redirecionam para a app (as únicas que são HTML)."""
    challenges = CHALLENGES if challenges is None else challenges
    creator = Creator(path).config_indexing(False, "eng")
    with creator:
        creator.add_item(
            _Entry("content/curriculum/index.json", json.dumps(INDEX), "application/json")
        )
        creator.add_item(
            _Entry("content/locales/intro.json", json.dumps(INTRO), "application/json")
        )
        for relative, text in challenges.items():
            creator.add_item(
                _Entry(f"content/curriculum/testcurso/{relative}.md", text, "text/markdown")
            )
            shell = f"<html><head><title>{relative}</title></head><body></body></html>"
            creator.add_item(_Entry(f"index/testcurso/{relative}.html", shell, "text/html", True))
        creator.add_item(_Entry("index.html", "<html><body>app</body></html>", "text/html", True))
        creator.set_mainpath("index.html")
        for key, value in {
            "Name": name,
            "Title": "FreeCodeCamp (teste)",
            "Language": "eng",
            "Date": date,
            "Creator": "freeCodeCamp",
            "Publisher": "openZIM",
            "Description": "Cursos de teste",
            "Tags": "FCC;freeCodeCamp",
        }.items():
            creator.add_metadata(key, value)
    return path
