"""Embeddings: a interface `Embedder` e as suas implementações (onnx, http, fake)."""

import threading

from ariadne.config import Settings
from ariadne.embedder.base import (
    EMBEDDING_DIM,
    Embedder,
    EmbedderError,
    make_model_id,
)
from ariadne.embedder.fake import FakeEmbedder
from ariadne.embedder.http import HttpEmbedder
from ariadne.embedder.onnx import OnnxEmbedder

__all__ = [
    "EMBEDDING_DIM",
    "Embedder",
    "EmbedderError",
    "FakeEmbedder",
    "HttpEmbedder",
    "OnnxEmbedder",
    "get_embedder",
    "make_model_id",
]

_cache: dict[tuple[object, ...], Embedder] = {}
_lock = threading.Lock()


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedder == "fake":
        return FakeEmbedder()
    if settings.embedder == "onnx":
        return OnnxEmbedder(
            settings.embedder_model,
            settings.embedder_revision,
            settings.models_dir,
            variant=settings.embedder_variant,
            threads=settings.embedder_threads,
            batch=settings.embedder_batch,
        )
    # llamacpp e http falam a mesma API (/v1/embeddings); o model_id tem de ser explícito porque
    # o servidor não diz que revisão do modelo está a servir.
    if not settings.embedder_url:
        raise EmbedderError(f"EMBEDDER={settings.embedder} needs EMBEDDER_URL.")
    if not settings.embedder_model_id:
        raise EmbedderError(f"EMBEDDER={settings.embedder} needs EMBEDDER_MODEL_ID.")
    return HttpEmbedder(
        settings.embedder_url,
        settings.embedder_model_id,
        model_name=settings.embedder_model,
        batch=settings.embedder_batch,
    )


def get_embedder(settings: Settings) -> Embedder:
    """Um embedder por processo e por configuração; o modelo carrega-se à primeira chamada."""
    key = (
        settings.embedder,
        settings.embedder_model,
        settings.embedder_revision,
        settings.embedder_variant,
        settings.embedder_threads,
        settings.embedder_url,
        settings.embedder_model_id,
        settings.models_dir,
    )
    with _lock:
        embedder = _cache.get(key)
        if embedder is None:
            embedder = _cache[key] = build_embedder(settings)
        return embedder
