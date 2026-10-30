"""Embedder por HTTP: endpoint compatível com a API da OpenAI (`/v1/embeddings`).

Cobre o llama-server (llama.cpp) e o plano B da secção 9.3 (embeddings delegados a outra máquina).
"""

import time
from collections.abc import Sequence

import httpx

from ariadne.embedder.base import EmbedderError, check_dim, normalize, prefixes

# Uma pergunta não pode ficar à espera de um endpoint morto; a indexação em lote tolera mais.
QUERY_TIMEOUT = httpx.Timeout(10.0, connect=2.0)
PASSAGE_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
RETRY_SECONDS = 60.0  # depois de uma falha, só se volta a tentar passado este tempo


class HttpEmbedder:
    def __init__(
        self,
        url: str,
        model_id: str,
        *,
        model_name: str = "",
        batch: int = 16,
        client: httpx.Client | None = None,
    ) -> None:
        base = url.rstrip("/")
        self._url = base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")
        self._model_id = model_id
        self._query_prefix, self._passage_prefix = prefixes(model_name or model_id)
        self._batch = max(1, batch)
        self._client = client or httpx.Client()
        self._dim: int | None = None
        self._last_error: EmbedderError | None = None
        self._retry_after = 0.0

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        if self._dim is None:
            self.embed_queries(["dim"])  # o primeiro pedido descobre e confere a dimensão
        assert self._dim is not None
        return self._dim

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self._query_prefix + t for t in texts], QUERY_TIMEOUT)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self._passage_prefix + t for t in texts], PASSAGE_TIMEOUT)

    def _embed(self, texts: list[str], timeout: httpx.Timeout) -> list[list[float]]:
        if self._last_error is not None and time.monotonic() < self._retry_after:
            raise self._last_error  # endpoint em baixo: não gastar timeouts a cada pedido
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), self._batch):
                vectors.extend(self._post(texts[start : start + self._batch], timeout))
        except EmbedderError as exc:
            self._last_error, self._retry_after = exc, time.monotonic() + RETRY_SECONDS
            raise
        self._last_error = None
        return vectors

    def _post(self, texts: list[str], timeout: httpx.Timeout) -> list[list[float]]:
        try:
            response = self._client.post(
                self._url, json={"model": self._model_id, "input": texts}, timeout=timeout
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors = [normalize(item["embedding"]) for item in data]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbedderError(f"HTTP embedder unavailable at {self._url}: {exc}") from exc
        if len(vectors) != len(texts):
            raise EmbedderError(
                f"The embedder returned {len(vectors)} vectors for {len(texts)} texts."
            )
        if vectors:
            if self._dim is None:
                check_dim(self._model_id, len(vectors[0]))
                self._dim = len(vectors[0])
            elif len(vectors[0]) != self._dim:
                raise EmbedderError("The vector dimension changed midway.")
        return vectors
