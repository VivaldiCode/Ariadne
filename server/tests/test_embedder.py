import hashlib
import io
import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import httpx
import pytest

from ariadne.config import Settings
from ariadne.embedder import (
    EMBEDDING_DIM,
    EmbedderError,
    FakeEmbedder,
    HttpEmbedder,
    OnnxEmbedder,
    get_embedder,
    make_model_id,
)
from ariadne.embedder import onnx as onnx_module
from ariadne.embedder.onnx import ensure_file, model_dir

REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
REPO = "intfloat/multilingual-e5-small"


def settings(**overrides: Any) -> Settings:
    return Settings(database_url="postgresql+psycopg://x@localhost/x", **overrides)


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


# --- model_id --------------------------------------------------------------------------------


def test_model_id_leva_a_revisao_e_a_variante() -> None:
    assert make_model_id(REPO, REVISION) == "intfloat/multilingual-e5-small@614241f"
    assert make_model_id(REPO, REVISION, "fp32") == "intfloat/multilingual-e5-small@614241f"
    assert make_model_id(REPO, REVISION, "int8") == "intfloat/multilingual-e5-small@614241f:int8"


def test_onnx_embedder_conhece_o_model_id_sem_carregar_o_modelo(tmp_path: Path) -> None:
    embedder = OnnxEmbedder(REPO, REVISION, tmp_path, variant="int8")
    assert embedder.model_id == "intfloat/multilingual-e5-small@614241f:int8"
    assert not any(tmp_path.iterdir())  # nada descarregado


def test_variante_desconhecida(tmp_path: Path) -> None:
    with pytest.raises(EmbedderError, match="Unknown variant"):
        OnnxEmbedder(REPO, REVISION, tmp_path, variant="fp16")


# --- fábrica ---------------------------------------------------------------------------------


def test_fabrica_devolve_o_mesmo_embedder_por_configuracao(tmp_path: Path) -> None:
    a = get_embedder(settings(embedder="onnx", models_dir=tmp_path))
    b = get_embedder(settings(embedder="onnx", models_dir=tmp_path))
    c = get_embedder(settings(embedder="onnx", models_dir=tmp_path, embedder_variant="int8"))
    assert a is b
    assert a is not c and c.model_id.endswith(":int8")


def test_http_e_llamacpp_exigem_url_e_model_id() -> None:
    with pytest.raises(EmbedderError, match="EMBEDDER_URL"):
        get_embedder(settings(embedder="http"))
    with pytest.raises(EmbedderError, match="EMBEDDER_MODEL_ID"):
        get_embedder(settings(embedder="llamacpp", embedder_url="http://llama:8080"))
    ok = get_embedder(
        settings(embedder="llamacpp", embedder_url="http://llama:8080", embedder_model_id="m@abc")
    )
    assert ok.model_id == "m@abc"


# --- fake ------------------------------------------------------------------------------------


def test_fake_e_determinista_e_normalizado() -> None:
    fake = FakeEmbedder()
    a, b, c = fake.embed_passages(["gato", "gato", "cão"])
    assert a == b and a != c
    assert len(a) == fake.dim == EMBEDDING_DIM
    assert norm(a) == pytest.approx(1.0)
    assert abs(sum(x * y for x, y in zip(a, c, strict=True))) < 0.3


def test_fake_usa_os_vetores_fixados() -> None:
    fake = FakeEmbedder(vectors={"q": [1.0] + [0.0] * (EMBEDDING_DIM - 1)})
    assert fake.embed_queries(["q"])[0][0] == 1.0
    assert fake.calls == [("query", ["q"])]


# --- http ------------------------------------------------------------------------------------


def http_embedder(handler: Any, **kwargs: Any) -> HttpEmbedder:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpEmbedder("http://llama:8080", "m@abc", client=client, **kwargs)


def test_http_chama_v1_embeddings_com_prefixos_e_lotes() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"url": str(request.url), **body})
        data = [
            {"index": i, "embedding": [3.0, 4.0] + [0.0] * (EMBEDDING_DIM - 2)}
            for i in reversed(range(len(body["input"])))  # fora de ordem de propósito
        ]
        return httpx.Response(200, json={"data": data})

    embedder = http_embedder(handler, model_name="intfloat/multilingual-e5-small", batch=2)
    vectors = embedder.embed_passages(["a", "b", "c"])

    assert [call["input"] for call in seen] == [["passage: a", "passage: b"], ["passage: c"]]
    assert seen[0]["url"] == "http://llama:8080/v1/embeddings"
    assert seen[0]["model"] == "m@abc"
    assert len(vectors) == 3 and vectors[0][:2] == pytest.approx([0.6, 0.8])  # normalizado
    assert embedder.embed_queries(["q"]) and seen[-1]["input"] == ["query: q"]
    assert embedder.dim == EMBEDDING_DIM


def test_http_falhas_viram_embedder_error() -> None:
    with pytest.raises(EmbedderError, match="unavailable"):
        http_embedder(lambda r: httpx.Response(500)).embed_queries(["x"])
    with pytest.raises(EmbedderError):
        http_embedder(lambda r: httpx.Response(200, json={"oops": 1})).embed_queries(["x"])

    def wrong_dim(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    with pytest.raises(EmbedderError, match="dimensions"):
        http_embedder(wrong_dim).embed_queries(["x"])


# --- descarga atómica --------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(
        self, data: bytes, headers: dict[str, str] | None = None, final_url: str = ""
    ) -> None:
        super().__init__(data)
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}
        self.final_url = final_url  # "" = vindo de outro sítio (redirecionado)

    def geturl(self) -> str:
        return self.final_url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_descarga_escreve_no_sitio_certo_sem_deixar_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []

    def fake_urlopen(url: str, timeout: float = 0) -> FakeResponse:
        urls.append(url)
        return FakeResponse(b"x" * 3_000_000)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    path = ensure_file(REPO, REVISION, "onnx/model.onnx", tmp_path)

    assert path == model_dir(tmp_path, REPO, REVISION) / "onnx" / "model.onnx"
    assert path.stat().st_size == 3_000_000
    assert urls == [f"https://huggingface.co/{REPO}/resolve/{REVISION}/onnx/model.onnx"]
    assert sorted(p.name for p in path.parent.iterdir()) == ["model.onnx", "model.onnx.meta"]
    assert ensure_file(REPO, REVISION, "onnx/model.onnx", tmp_path) == path
    assert len(urls) == 1  # já existe: não volta a descarregar


def test_descarga_apaga_restos_antigos_de_descargas_interrompidas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = model_dir(tmp_path, REPO, REVISION)
    folder.mkdir(parents=True)
    old, recent = folder / "tokenizer.json.abc.part", folder / "tokenizer.json.def.part"
    old.write_bytes(b"x")
    recent.write_bytes(b"x")
    os.utime(old, (0, 0))  # de 1970
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: FakeResponse(b"{}"))

    ensure_file(REPO, REVISION, "tokenizer.json", tmp_path)

    assert not old.exists() and recent.exists()


def test_descarga_que_falha_nao_deixa_ficheiros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            raise OSError("ligação cortada")

    def failing_urlopen(url: str, timeout: float = 0) -> FakeResponse:
        return Broken(b"")

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(EmbedderError, match="Could not fetch"):
        ensure_file(REPO, REVISION, "tokenizer.json", tmp_path)
    assert list(model_dir(tmp_path, REPO, REVISION).iterdir()) == []

    def offline(url: str, timeout: float = 0) -> FakeResponse:
        raise urllib.error.URLError("sem rede")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(EmbedderError):
        ensure_file(REPO, REVISION, "tokenizer.json", tmp_path)
    assert list(model_dir(tmp_path, REPO, REVISION).iterdir()) == []


# --- carregamento ----------------------------------------------------------------------------


def test_modelo_que_nao_descarrega_nao_e_tentado_a_cada_pergunta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def offline(url: str, timeout: float = 0) -> FakeResponse:
        attempts.append(url)
        raise urllib.error.URLError("sem rede")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    embedder = OnnxEmbedder(REPO, REVISION, tmp_path)

    for _ in range(3):
        with pytest.raises(EmbedderError):
            embedder.embed_passages(["x"])
    assert len(attempts) == 1  # as outras duas dentro do período de espera


def test_pergunta_nao_espera_pelo_download_mas_a_passagem_espera(tmp_path: Path) -> None:
    embedder = OnnxEmbedder(REPO, REVISION, tmp_path)
    embedder._lock.acquire()  # outro fio está a descarregar o modelo
    try:
        with pytest.raises(EmbedderError, match="still downloading"):
            embedder.embed_queries(["x"])
    finally:
        embedder._lock.release()


def test_descarga_truncada_ou_corrompida_nao_e_promovida(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"conteudo do modelo"
    good_sha = hashlib.sha256(data).hexdigest()
    folder = model_dir(tmp_path, REPO, REVISION)
    cases = {
        "truncada": {"Content-Length": str(len(data) + 5)},
        "sha": {"Content-Length": str(len(data)), "X-Linked-ETag": f'"{"0" * 64}"'},
    }
    for headers in cases.values():
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda url, timeout=0, h=headers: FakeResponse(data, h)
        )
        with pytest.raises(EmbedderError, match=r"Truncated|Corrupted"):
            ensure_file(REPO, REVISION, "tokenizer.json", tmp_path)
        assert list(folder.iterdir()) == []  # nem ficheiro final nem .part

    ok = {"Content-Length": str(len(data)), "X-Linked-ETag": f'"{good_sha}"'}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: FakeResponse(data, ok))
    path = ensure_file(REPO, REVISION, "tokenizer.json", tmp_path)
    assert path.read_bytes() == data
    # ETag de 40 hex (sha1 git, ficheiros pequenos) não é um sha256: ignora-se.
    sha1 = {"Content-Length": str(len(data)), "ETag": '"' + "a" * 40 + '"'}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: FakeResponse(data, sha1))
    assert ensure_file(REPO, REVISION, "other.json", tmp_path).read_bytes() == data


def test_ficheiro_existente_com_tamanho_errado_volta_a_ser_descarregado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_urlopen(url: str, timeout: float = 0) -> FakeResponse:
        calls.append(url)
        return FakeResponse(b"completo")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    path = ensure_file(REPO, REVISION, "tokenizer.json", tmp_path)
    path.write_bytes(b"trunc")  # alguém truncou-o depois (disco cheio, cópia interrompida)
    assert ensure_file(REPO, REVISION, "tokenizer.json", tmp_path).read_bytes() == b"completo"
    assert len(calls) == 2
    # Copiado à mão, sem .meta: aceita-se tal como está.
    path.with_name(path.name + ".meta").unlink()
    path.write_bytes(b"copiado")
    assert ensure_file(REPO, REVISION, "tokenizer.json", tmp_path).read_bytes() == b"copiado"
    assert len(calls) == 2


def test_perguntas_nunca_descarregam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(url: str, timeout: float = 0) -> FakeResponse:
        raise AssertionError("uma pergunta tentou descarregar")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    embedder = OnnxEmbedder(REPO, REVISION, tmp_path)
    with pytest.raises(EmbedderError, match="has not been downloaded yet"):
        embedder.embed_queries(["x"])
    with pytest.raises(EmbedderError):  # e repete-se sem cooldown: é só uma verificação ao disco
        embedder.embed_queries(["x"])


def test_http_timeouts_curtos_nas_perguntas_e_recuo_depois_de_falhar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[httpx.Timeout] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(500)

    embedder = http_embedder(handler)
    with pytest.raises(EmbedderError):
        embedder.embed_queries(["x"])
    assert timeouts[0] == {"connect": 2.0, "read": 10.0, "write": 10.0, "pool": 10.0}
    with pytest.raises(EmbedderError):  # dentro dos 60 s: nem chega ao endpoint
        embedder.embed_queries(["x"])
    with pytest.raises(EmbedderError):
        embedder.embed_passages(["x"])
    assert calls["n"] == 1

    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 61)
    with pytest.raises(EmbedderError):
        embedder.embed_passages(["x"])  # passado o recuo, tenta outra vez (timeout longo)
    assert calls["n"] == 2 and timeouts[1]["read"] == 120.0


@pytest.fixture(autouse=True)
def _sem_rede_para_o_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Os testes nunca fazem o HEAD real ao huggingface.co."""
    monkeypatch.setattr(onnx_module, "_hub_metadata", lambda url: (None, None))


def test_sha256_vem_do_hub_e_nao_do_etag_da_cdn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Com o Xet, a CDN responde com um ETag de 64 hex que NÃO é o sha256 do ficheiro."""
    data = b"modelo"
    good = hashlib.sha256(data).hexdigest()
    cdn = {"Content-Length": str(len(data)), "ETag": f'"{"c" * 64}"'}  # hash Xet, não sha256
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: FakeResponse(data, cdn))

    monkeypatch.setattr(onnx_module, "_hub_metadata", lambda url: (len(data), good))
    assert ensure_file("org/modelo", "abc", "onnx/model.onnx", tmp_path).read_bytes() == data

    other = tmp_path / "outra"
    monkeypatch.setattr(onnx_module, "_hub_metadata", lambda url: (len(data), "0" * 64))
    with pytest.raises(EmbedderError, match="sha256"):
        ensure_file("org/modelo", "abc", "onnx/model.onnx", other)
