"""Embedder local: onnxruntime (CPU) + tokenizers, com mean pooling (e5).

Corre em CPU sem AVX (X5550): a lógica é a do `bench/embeddings/bench.py`. Os ficheiros vêm do
Hugging Face na primeira utilização, numa revisão fixa, para `MODELS_DIR`.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ariadne.embedder.base import EmbedderError, check_dim, make_model_id, prefixes

logger = logging.getLogger(__name__)

TOKENIZER_FILE = "tokenizer.json"
META_SUFFIX = ".meta"
VARIANT_FILES = {
    "fp32": "onnx/model.onnx",
    "int8": "onnx/model_qint8_avx512_vnni.onnx",
}
MAX_TOKENS = 512
DOWNLOAD_TIMEOUT = 60
STALE_PART_SECONDS = 3600.0
RETRY_SECONDS = 60.0  # depois de uma falha a carregar, só se volta a tentar passado este tempo


def model_dir(models_dir: Path, repo: str, revision: str) -> Path:
    """A mesma disposição do benchmark: <models>/<repo com -->/<revisão de 12 caracteres>."""
    return models_dir / repo.replace("/", "--") / revision[:12]


def ensure_file(
    repo: str, revision: str, filename: str, models_dir: Path, *, download: bool = True
) -> Path:
    """O ficheiro do modelo, descarregado se faltar ou estiver errado.

    A descarga é atómica (.part -> rename) e só é promovida se os bytes recebidos batem com o
    `Content-Length` e, quando o Hugging Face o dá (`X-Linked-ETag`/`ETag` com 64 hex), com o
    sha256. Ao lado fica `<ficheiro>.meta` com o tamanho: um ficheiro que já exista com outro
    tamanho (truncado) volta a ser descarregado. Com `download=False` só se usa o que está em
    disco (é o caso das perguntas, que nunca descarregam).
    """
    target = model_dir(models_dir, repo, revision) / filename
    meta = target.with_name(target.name + META_SUFFIX)
    if _is_complete(target, meta):
        return target
    url = f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"
    if not download:
        raise EmbedderError(f"The model has not been downloaded yet ({target}).")
    target.parent.mkdir(parents=True, exist_ok=True)
    _remove_stale_parts(target)
    logger.info("Downloading %s to %s", url, target)
    part = tempfile.NamedTemporaryFile(  # noqa: SIM115 - fechado no finally
        dir=target.parent, prefix=target.name + ".", suffix=".part", delete=False
    )
    # O tamanho e o sha256 certos vêm do 1.º salto (huggingface.co, X-Linked-Size/X-Linked-Etag):
    # a resposta final é da CDN, cujo ETag (Xet) é outro hash de 64 hex.
    hub_size, hub_sha = _hub_metadata(url)
    try:
        # O nome temporário é único: o server e o worker podem descarregar ao mesmo tempo.
        with part, urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
            digest = hashlib.sha256()
            size = 0
            while block := response.read(1 << 20):
                part.write(block)
                digest.update(block)
                size += len(block)
            redirected = response.geturl() != url
            expected_size = hub_size or _int_or_none(response.headers.get("Content-Length"))
            expected_sha = hub_sha or _sha256_from(response.headers, etag=not redirected)
        if size == 0 or (expected_size is not None and size != expected_size):
            raise EmbedderError(f"Truncated download of {url}: {size} of {expected_size} bytes.")
        if expected_sha is not None and digest.hexdigest() != expected_sha:
            raise EmbedderError(f"Corrupted download of {url}: the sha256 does not match.")
        os.replace(part.name, target)
        meta.write_text(json.dumps({"size": size, "sha256": digest.hexdigest()}))
    except (urllib.error.URLError, OSError) as exc:
        raise EmbedderError(f"Could not fetch {url}: {exc}") from exc
    finally:
        Path(part.name).unlink(missing_ok=True)
    return target


def _is_complete(target: Path, meta: Path) -> bool:
    try:
        size = target.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    try:
        expected = json.loads(meta.read_text())["size"]
    except (OSError, ValueError, KeyError, TypeError):
        return True  # sem .meta (copiado à mão): aceita-se
    return bool(expected == size)


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _hub_metadata(url: str) -> tuple[int | None, str | None]:
    """(tamanho, sha256) que o Hugging Face anuncia no 1.º salto, sem seguir a CDN; (None, None)
    se não der (sem rede, outro servidor): fica a verificação pela resposta da descarga."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=30)
        headers = response.headers
    except urllib.error.HTTPError as exc:  # o 302 da CDN chega aqui como "erro"
        headers = exc.headers
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    size = _int_or_none(headers.get("X-Linked-Size"))
    linked = (headers.get("X-Linked-Etag") or "").removeprefix("W/").strip('"')
    return size, linked if re.fullmatch(r"[0-9a-f]{64}", linked) else None


def _sha256_from(headers: Any, *, etag: bool = True) -> str | None:
    """O sha256 dos ficheiros LFS vem em `X-Linked-ETag`; o `ETag` só conta se a resposta não foi
    redirecionada (o da CDN do Hugging Face é um hash Xet, não o sha256 do ficheiro)."""
    for name in ("X-Linked-ETag", "ETag") if etag else ("X-Linked-ETag",):
        value = (headers.get(name) or "").removeprefix("W/").strip('"')
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return None


def _remove_stale_parts(target: Path) -> None:
    """Apaga restos de descargas interrompidas (processo morto a meio). Os recentes ficam: podem
    ser de outro processo a descarregar neste momento."""
    for part in target.parent.glob(f"{target.name}.*.part"):
        try:
            if time.time() - part.stat().st_mtime > STALE_PART_SECONDS:
                part.unlink(missing_ok=True)
        except OSError:
            continue


class OnnxEmbedder:
    """Carrega o modelo à primeira utilização (o `model_id` não precisa dele)."""

    def __init__(
        self,
        repo: str,
        revision: str,
        models_dir: Path,
        *,
        variant: str = "fp32",
        threads: int = 0,
        batch: int = 16,
    ) -> None:
        if variant not in VARIANT_FILES:
            raise EmbedderError(f"Unknown variant: {variant!r} (fp32 or int8).")
        self._repo, self._revision, self._models_dir = repo, revision, models_dir
        self._variant, self._threads, self._batch = variant, threads, max(1, batch)
        self._model_id = make_model_id(repo, revision, variant)
        self._query_prefix, self._passage_prefix = prefixes(repo)
        self._lock = threading.Lock()
        self._loaded = False
        self._last_error: EmbedderError | None = None
        self._retry_after = 0.0
        self._dim: int | None = None
        self._np: Any = None
        self._session: Any = None
        self._tokenizer: Any = None
        self._inputs: set[str] = set()
        self._output = ""

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        self._load()
        assert self._dim is not None
        return self._dim

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        # Uma pergunta nunca descarrega nem espera por quem descarrega: carrega o que já está em
        # disco ou falha já, e o router usa o nível 1. Descarregam o `warm_embedder` e o worker.
        return self._embed([self._query_prefix + t for t in texts], wait=False, download=False)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self._passage_prefix + t for t in texts], wait=True, download=True)

    def _load(self, wait: bool = True, download: bool = True) -> None:
        if self._loaded:
            return
        if not self._lock.acquire(blocking=wait):
            raise EmbedderError("The model is still downloading/loading.")
        try:
            if self._loaded:
                return
            if download and self._last_error is not None and time.monotonic() < self._retry_after:
                raise self._last_error  # não repetir a descarga a cada pergunta
            try:
                self._load_model(download)
            except EmbedderError as exc:
                if download:  # sem descarga a falha é só «ainda não está em disco»: barata
                    self._last_error, self._retry_after = exc, time.monotonic() + RETRY_SECONDS
                raise
        finally:
            self._lock.release()

    def _load_model(self, download: bool) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:  # pragma: no cover - dependências do servidor
            raise EmbedderError(f"A dependency of the ONNX embedder is missing: {exc}") from exc
        model = ensure_file(
            self._repo,
            self._revision,
            VARIANT_FILES[self._variant],
            self._models_dir,
            download=download,
        )
        tokenizer_path = ensure_file(
            self._repo, self._revision, TOKENIZER_FILE, self._models_dir, download=download
        )
        try:
            options = ort.SessionOptions()
            options.intra_op_num_threads = self._threads
            options.inter_op_num_threads = 1
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
        except Exception as exc:
            raise EmbedderError(f"Could not load the model {model}: {exc}") from exc
        tokenizer.enable_truncation(max_length=MAX_TOKENS)
        tokenizer.enable_padding(pad_id=tokenizer.token_to_id("<pad>"), pad_token="<pad>")
        outputs = [o.name for o in session.get_outputs()]
        self._output = "last_hidden_state" if "last_hidden_state" in outputs else outputs[0]
        self._inputs = {i.name for i in session.get_inputs()}
        self._np, self._session, self._tokenizer = np, session, tokenizer
        dim = int(session.get_outputs()[0].shape[-1])
        check_dim(self._model_id, dim)
        self._dim = dim
        self._loaded = True
        logger.info("Embedder %s loaded (onnxruntime %s)", self._model_id, ort.__version__)

    def _embed(self, texts: list[str], wait: bool, download: bool) -> list[list[float]]:
        self._load(wait, download)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch):
            vectors.extend(self._run(texts[start : start + self._batch]))
        return vectors

    def _run(self, texts: list[str]) -> list[list[float]]:
        np = self._np
        try:
            encodings = self._tokenizer.encode_batch(texts)
            ids = np.array([e.ids for e in encodings], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run([self._output], feeds)[0]
            if hidden.ndim == 3:  # mean pooling (e5)
                weights = mask[..., None].astype(hidden.dtype)
                hidden = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
            hidden = hidden / np.clip(np.linalg.norm(hidden, axis=1, keepdims=True), 1e-12, None)
            return [[float(x) for x in row] for row in hidden.tolist()]
        except Exception as exc:
            raise EmbedderError(f"Failed to compute embeddings: {exc}") from exc
