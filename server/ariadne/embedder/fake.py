"""Embedder determinístico para testes: o vetor sai de um hash do texto."""

import hashlib
import random
from collections.abc import Mapping, Sequence

from ariadne.embedder.base import EMBEDDING_DIM, EmbedderError, normalize


class FakeEmbedder:
    """Textos iguais dão vetores iguais e textos diferentes dão vetores quase ortogonais.

    `vectors` fixa o vetor de textos concretos (sem prefixo), para os testes controlarem as
    semelhanças. Conta as chamadas em `calls` e pode ser posto a falhar com `failing`.
    """

    def __init__(
        self,
        model_id: str = "fake/embedder@0000000",
        vectors: Mapping[str, Sequence[float]] | None = None,
    ) -> None:
        self._model_id = model_id
        self.vectors: dict[str, list[float]] = {k: list(v) for k, v in (vectors or {}).items()}
        self.calls: list[tuple[str, list[str]]] = []
        self.failing = False

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed("query", texts)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed("passage", texts)

    def _embed(self, kind: str, texts: Sequence[str]) -> list[list[float]]:
        if self.failing:
            raise EmbedderError("toy embedder unavailable")
        self.calls.append((kind, list(texts)))
        return [self.vectors.get(text) or hash_vector(text) for text in texts]


def hash_vector(text: str) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    return normalize([rng.gauss(0, 1) for _ in range(EMBEDDING_DIM)])
