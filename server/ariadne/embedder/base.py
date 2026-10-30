"""Interface `Embedder` (ARCHITECTURE.md, secção 14, ponto 9) e o que é comum às implementações."""

import math
from collections.abc import Sequence
from typing import Protocol

# Dimensão das colunas `vector(384)`; outro modelo com outra dimensão pede tabela própria.
EMBEDDING_DIM = 384

E5_QUERY_PREFIX = "query: "
E5_PASSAGE_PREFIX = "passage: "


class EmbedderError(Exception):
    """O modelo não está disponível ou respondeu mal: o router degrada para o nível 1."""


class Embedder(Protocol):
    """Transforma texto em vetores L2-normalizados de `dim` dimensões.

    Os prefixos do modelo (e5: `query: ` / `passage: `) são aplicados aqui dentro: quem chama passa
    texto simples. `model_id` (`<repo>@<revisão>`) vai para cada vetor guardado.
    """

    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


def make_model_id(repo: str, revision: str, variant: str = "fp32") -> str:
    """'intfloat/multilingual-e5-small@614241f'; a variante quantizada dá outros vetores."""
    model_id = f"{repo}@{revision[:7]}"
    return model_id if variant == "fp32" else f"{model_id}:{variant}"


def prefixes(model_name: str) -> tuple[str, str]:
    """(consulta, passagem). Só a família e5 os usa."""
    if "e5" in model_name.lower():
        return E5_QUERY_PREFIX, E5_PASSAGE_PREFIX
    return "", ""


def normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise EmbedderError("The model returned a zero vector.")
    return [x / norm for x in vector]


def check_dim(model_id: str, dim: int) -> None:
    if dim != EMBEDDING_DIM:
        raise EmbedderError(
            f"Model {model_id} has {dim} dimensions and the tables are vector({EMBEDDING_DIM}): "
            "another dimension needs its own table (section 10)."
        )
