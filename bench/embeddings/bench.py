#!/usr/bin/env python3
"""Ariadne: benchmark de embeddings em CPU sem AVX (Fase 0, ARCHITECTURE.md secção 9.3).

Mede frases/s e latência do multilingual-e5-small com onnxruntime e com llama.cpp compilado
sem AVX/AVX2/FMA/F16C/BMI2, e verifica se as bibliotecas nativas rebentam com
«illegal instruction» num CPU antigo (Xeon X5550, só SSE4.2).

O processo principal usa só a biblioteca padrão. Tudo o que é nativo (numpy, onnxruntime,
tokenizers, libzim, llama-server) corre em subprocessos, para que um SIGILL fique registado
no relatório em vez de matar o benchmark.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

BENCH_VERSION = "1"

E5_REPO = "intfloat/multilingual-e5-small"
E5_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"

# (backend, variante) -> ficheiro. Os ONNX vêm do repositório oficial; os GGUF são convertidos
# na imagem (convert_e5.py) e procurados em /data/gguf, depois em /opt/gguf.
VARIANTS: dict[tuple[str, str], str] = {
    ("ort", "fp32"): "onnx/model.onnx",
    ("ort", "int8"): "onnx/model_qint8_avx512_vnni.onnx",
    ("llamacpp", "f32"): "multilingual-e5-small-f32.gguf",
    ("llamacpp", "q8_0"): "multilingual-e5-small-q8_0.gguf",
}
GGUF_DIRS = (Path("/opt/gguf"),)  # /data/gguf é acrescentado à frente em main()
REFERENCE = ("ort", "fp32")
DAILY_BUDGET = 20_000  # chunks por dia (secção 6.4)
CPU_FLAGS = ("sse4_2", "popcnt", "avx", "avx2", "fma", "f16c", "bmi2", "avx512f")
SIGILL_SHELL_RC = 128 + signal.SIGILL  # quando o processo morre debaixo de uma shell

CANARIES = {
    "numpy": ("import numpy as np; a = np.ones(4096, dtype=np.float32); print(np.__version__, float(a @ a))"),
    "onnxruntime": "import onnxruntime as ort; print(ort.__version__)",
    "tokenizers": "import tokenizers; print(tokenizers.__version__)",
    "libzim": ("from importlib.metadata import version; import libzim.reader; print(version('libzim'))"),
}

# --- Textos (PT/EN, escritos para este benchmark) ----------------------------------------

SANITY = {
    "pt": (
        "passage: A Revolução dos Cravos foi o golpe militar de 25 de Abril de 1974 que derrubou "
        "o Estado Novo em Portugal e abriu caminho à democracia."
    ),
    "en": (
        "passage: The Carnation Revolution was the military coup of 25 April 1974 that overthrew "
        "the Estado Novo regime in Portugal and opened the way to democracy."
    ),
    "outro": (
        "passage: Para fazer pão caseiro mistura-se farinha, água, sal e fermento, amassa-se bem "
        "e deixa-se a massa levedar durante algumas horas."
    ),
}

QUERIES = [
    "query: quando foi a Revolução dos Cravos em Portugal",
    "query: how does photosynthesis turn sunlight into chemical energy",
    "query: qual é a capital de Cabo Verde",
    "query: python list comprehension with a condition",
    "query: causas da queda do Império Romano do Ocidente",
    "query: what is the boiling point of water at high altitude",
    "query: como funciona um motor de combustão interna",
    "query: who wrote the first modern novel in Spanish",
    "query: diferença entre vírus e bactéria",
    "query: how to configure a reverse proxy with nginx",
    "query: quem foi Luís de Camões e o que escreveu",
    "query: why do leaves change colour in autumn",
]

SENTENCES_PT = [
    "O rio Douro nasce em Espanha e atravessa o norte de Portugal até desaguar no Porto.",
    "As caravelas permitiram aos navegadores portugueses explorar a costa africana no século XV.",
    "A fotossíntese transforma a energia da luz em açúcares que alimentam as plantas.",
    "Um compilador traduz código-fonte numa linguagem que o processador consegue executar.",
    "O terramoto de 1755 destruiu grande parte de Lisboa e mudou o urbanismo da cidade.",
    "As bases de dados relacionais organizam a informação em tabelas ligadas por chaves.",
    "O fado é um género musical português classificado como património imaterial da humanidade.",
    "A vacinação reduziu drasticamente a mortalidade infantil ao longo do século XX.",
    "Os Açores são um arquipélago de nove ilhas vulcânicas no meio do Atlântico Norte.",
    "Uma rede neuronal ajusta os seus pesos para reduzir o erro entre a previsão e o valor real.",
    "O azulejo chegou à Península Ibérica pela influência árabe e ganhou estilo próprio.",
    "A erosão costeira ameaça várias praias do litoral português todos os invernos.",
    "Os mosteiros medievais guardavam manuscritos copiados à mão durante gerações.",
    "A energia eólica já produz uma parte importante da eletricidade consumida no país.",
]

SENTENCES_EN = [
    "The printing press made books cheaper and helped spread new ideas across Europe.",
    "Plate tectonics explains why earthquakes cluster along the edges of continents.",
    "A hash table stores values in buckets chosen by a function of the key.",
    "The Industrial Revolution began in Britain with steam engines and textile mills.",
    "Coral reefs host a quarter of marine species while covering a tiny fraction of the ocean.",
    "Version control systems record every change so that teams can work on the same code.",
    "The Silk Road connected China to the Mediterranean through a web of trade routes.",
    "Antibiotics target bacteria but have no effect on viral infections such as the flu.",
    "Glaciers carve deep valleys as they slowly move downhill under their own weight.",
    "A transformer model uses attention to weigh how much each word relates to the others.",
    "Ancient libraries kept scrolls that scholars travelled great distances to consult.",
    "Solar panels convert sunlight directly into electricity using semiconductor cells.",
    "The Renaissance revived interest in classical art, science and philosophy.",
    "Caching keeps frequently used data close to the processor to avoid slow lookups.",
]


def build_passages(count: int = 32, words: int = 170) -> list[str]:
    """Passagens de ~256 tokens (o tamanho típico de um chunk), alternando PT e EN."""
    passages = []
    for i in range(count):
        pool = SENTENCES_PT if i % 2 == 0 else SENTENCES_EN
        parts: list[str] = []
        n_words, j = 0, (i * 3) % len(pool)
        while n_words < words:
            sentence = pool[j % len(pool)]
            parts.append(sentence)
            n_words += len(sentence.split())
            j += 1
        passages.append("passage: " + " ".join(parts))
    return passages


# --- Utilitários (só biblioteca padrão) ----------------------------------------------------


def log(msg: str) -> None:
    print(msg, flush=True)


def status_from_rc(rc: int) -> str:
    if rc == 0:
        return "ok"
    if rc in (-signal.SIGILL, SIGILL_SHELL_RC):
        return "ILLEGAL_INSTRUCTION"
    if rc < 0:
        return signal.Signals(-rc).name
    return "erro"


def run_isolated(cmd: list[str], timeout: float, log_path: Path | None = None) -> tuple[str, str]:
    """Corre um subprocesso no seu próprio grupo; devolve (estado, saída)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        status = status_from_rc(proc.returncode)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid)
        out, _ = proc.communicate()
        status = "timeout"
    finally:
        # Mata o que o subprocesso tenha deixado vivo (ex.: um llama-server que não arrancou).
        _kill_group(proc.pid)
    if log_path is not None:
        log_path.write_text(out)
    return status, out


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1.0)


def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "host": platform.node(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "cpu": platform.processor() or "?",
        "threads": os.cpu_count(),
        "threads_disponiveis": len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count(),
        "numa_nodes": len(list(Path("/sys/devices/system/node").glob("node[0-9]*"))) or None,
        "mem_gb": None,
        "flags": {},
    }
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        lines = cpuinfo.read_text().splitlines()
        model = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("model name")), None)
        flags = next((ln.split(":", 1)[1].split() for ln in lines if ln.startswith("flags")), [])
        info["cpu"] = model or info["cpu"]
        if platform.machine() in ("x86_64", "AMD64"):  # as flags só fazem sentido em x86
            info["flags"] = {flag: flag in flags for flag in CPU_FLAGS}
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        total_kb = next(
            int(ln.split()[1]) for ln in meminfo.read_text().splitlines() if ln.startswith("MemTotal")
        )
        info["mem_gb"] = round(total_kb / 1024**2, 1)
    elif sys.platform == "darwin":
        info["cpu"] = _sysctl("machdep.cpu.brand_string") or info["cpu"]
        info["mem_gb"] = round(int(_sysctl("hw.memsize") or 0) / 1024**3, 1)
    return info


def _sysctl(key: str) -> str:
    return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True).stdout.strip()


def fetch(models_dir: Path, repo: str, revision: str, filename: str) -> Path:
    """Descarrega um ficheiro do Hugging Face numa revisão fixa (com cache em models_dir)."""
    target = models_dir / repo.replace("/", "--") / revision[:12] / filename
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"
    request = urllib.request.Request(url, headers={"User-Agent": "ariadne-bench"})
    tmp = target.with_name(target.name + ".part")
    with urllib.request.urlopen(request, timeout=60) as resp, tmp.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        log(f"  a descarregar {repo}/{filename} ({total / 1e6:.0f} MB)…")
        shutil.copyfileobj(resp, fh, 1 << 20)
    if total and tmp.stat().st_size != total:
        raise RuntimeError(f"download incompleto: {url}")
    tmp.replace(target)
    return target


# --- Subprocesso de medição (aqui sim, código nativo) -------------------------------------


class ServerDiedError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class OrtEmbedder:
    def __init__(self, model_path: str, tokenizer_path: str, threads: int) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        start = time.perf_counter()
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        outputs = [o.name for o in self.session.get_outputs()]
        self.output = "last_hidden_state" if "last_hidden_state" in outputs else outputs[0]
        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_truncation(max_length=512)
        pad_id = self.tokenizer.token_to_id("<pad>")
        self.tokenizer.enable_padding(pad_id=pad_id, pad_token="<pad>")
        self.load_s = time.perf_counter() - start
        self.version = ort.__version__

    def embed(self, texts: list[str]) -> list[list[float]]:
        np = self.np
        encodings = self.tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run([self.output], feeds)[0]
        if hidden.ndim == 3:  # mean pooling (e5)
            weights = mask[..., None].astype(hidden.dtype)
            hidden = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        hidden = hidden / np.linalg.norm(hidden, axis=1, keepdims=True)
        return hidden.tolist()

    def count_tokens(self, texts: list[str]) -> list[int]:
        return [sum(e.attention_mask) for e in self.tokenizer.encode_batch(texts)]

    def close(self) -> None:
        pass


class HttpEmbedder:
    """Endpoint compatível com a API OpenAI (/v1/embeddings): llama-server ou plano B."""

    def __init__(self, base_url: str, model: str) -> None:
        base = base_url.rstrip("/")
        self.url = base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")
        self.model = model
        self.load_s = 0.0
        self.version: str | None = None

    def _post(self, url: str, payload: dict[str, Any], timeout: float = 900) -> Any:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.load(resp)

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post(self.url, {"input": texts, "model": self.model, "encoding_format": "float"})
        rows = sorted(data["data"], key=lambda row: row["index"])
        return [l2_normalize(row["embedding"]) for row in rows]

    def count_tokens(self, texts: list[str]) -> list[int] | None:
        return None

    def close(self) -> None:
        pass


class LlamaServerEmbedder(HttpEmbedder):
    def __init__(self, server_bin: str, gguf_path: str, threads: int, batch: int, log_path: str) -> None:
        with socket.socket() as sock:
            sock.bind(("localhost", 0))
            port = sock.getsockname()[1]
        # 512 tokens por slot, um slot por frase do lote; -ub 512 processa uma frase por micro-lote
        # (com -ub maior o llama.cpp calcula atenção entre frases diferentes, ~18% mais lento).
        ctx = 512 * batch
        cmd = [
            server_bin, "-m", gguf_path, "--embeddings", "--pooling", "mean",
            "-t", str(threads), "-tb", str(threads),
            "-np", str(batch), "-c", str(ctx), "-b", str(ctx), "-ub", "512",
            "--port", str(port),
        ]  # fmt: skip
        self.log_path = log_path
        self.log_file = open(log_path, "w")  # noqa: SIM115 (fecha-se em close())
        self.proc = subprocess.Popen(cmd, stdout=self.log_file, stderr=subprocess.STDOUT)
        self.base = f"http://localhost:{port}"
        super().__init__(self.base, "e5")
        try:
            self._wait_ready()
        except BaseException:
            self.close()
            raise
        version = subprocess.run([server_bin, "--version"], capture_output=True, text=True)
        self.version = (
            (version.stdout + version.stderr).strip().splitlines()[0] if version.returncode == 0 else None
        )

    def _wait_ready(self) -> None:
        start = time.perf_counter()
        while True:
            self._check_alive()
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=5) as resp:
                    if resp.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            if time.perf_counter() - start > 600:
                raise TimeoutError("llama-server não ficou pronto em 600 s")
            time.sleep(0.5)
        self.load_s = time.perf_counter() - start

    def _check_alive(self, grace: float = 0.0) -> None:
        try:
            rc = self.proc.wait(timeout=grace) if grace else self.proc.poll()
        except subprocess.TimeoutExpired:
            rc = None
        if rc is not None:
            raise ServerDiedError(status_from_rc(rc), f"llama-server terminou (rc={rc}); ver {self.log_path}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return super().embed(texts)
        except (urllib.error.URLError, ConnectionError):
            self._check_alive(grace=2.0)
            raise

    def count_tokens(self, texts: list[str]) -> list[int]:
        return [
            len(self._post(self.base + "/tokenize", {"content": t, "add_special": True})["tokens"])
            for t in texts
        ]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log_file.close()


def make_embedder(spec: dict[str, Any]) -> OrtEmbedder | HttpEmbedder:
    if spec["backend"] == "ort":
        return OrtEmbedder(spec["model_path"], spec["tokenizer_path"], spec["threads"])
    if spec["backend"] == "llamacpp":
        return LlamaServerEmbedder(
            spec["llama_server"], spec["model_path"], spec["threads"],
            spec["passage_batch"], spec["server_log"],
        )  # fmt: skip
    return HttpEmbedder(spec["http_url"], spec["http_model"])


def run_workloads(emb: OrtEmbedder | HttpEmbedder, spec: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    seconds = spec["seconds"]
    if "query" in spec["workloads"]:
        queries = spec["queries"]
        emb.embed([queries[0]])  # aquecimento
        latencies: list[float] = []
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline or not latencies:
            start = time.perf_counter()
            emb.embed([queries[len(latencies) % len(queries)]])
            latencies.append(time.perf_counter() - start)
        out["query"] = {
            "calls": len(latencies),
            "p50_ms": percentile(latencies, 50) * 1000,
            "p95_ms": percentile(latencies, 95) * 1000,
            "per_s": len(latencies) / sum(latencies),
        }
    if "passage" in spec["workloads"]:
        passages, batch = spec["passages"], spec["passage_batch"]
        emb.embed(passages[:2])  # aquecimento
        done, busy, offset = 0, 0.0, 0
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline or done == 0:
            chunk = [passages[(offset + k) % len(passages)] for k in range(batch)]
            offset += batch
            start = time.perf_counter()
            emb.embed(chunk)
            busy += time.perf_counter() - start
            done += batch
        tokens = emb.count_tokens(passages)
        tokens_per_passage = statistics.mean(tokens) if tokens else None
        out["passage"] = {
            "batch": batch,
            "calls": done // batch,
            "sentences_per_s": done / busy,
            "tokens_per_passage": tokens_per_passage,
            "tokens_per_s": done * tokens_per_passage / busy if tokens_per_passage else None,
        }
    return out


def worker_main(spec_path: str, result_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text())
    result: dict[str, Any]
    emb = None
    try:
        emb = make_embedder(spec)
        result = {"status": "ok", "load_s": emb.load_s, "runtime_version": emb.version}
        result.update(run_workloads(emb, spec))
        if spec["want_quality"]:
            result["quality_vectors"] = emb.embed(spec["quality_texts"])
    except ServerDiedError as exc:
        result = {"status": exc.status, "error": str(exc)}
    except Exception as exc:
        result = {"status": "erro", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if emb is not None:
            emb.close()
    Path(result_path).write_text(json.dumps(result))


ZIM_CODE = r"""
import json, sys, time
from libzim.reader import Archive
path, n = sys.argv[1], int(sys.argv[2])
start = time.perf_counter()
archive = Archive(path)
open_s = time.perf_counter() - start
size = 0
start = time.perf_counter()
for _ in range(n):
    entry = archive.get_random_entry()
    if entry.is_redirect:
        entry = entry.get_redirect_entry()
    size += len(bytes(entry.get_item().content))
read_s = time.perf_counter() - start
print(json.dumps({
    "articles": archive.article_count, "entries": archive.all_entry_count,
    "fulltext_index": archive.has_fulltext_index, "open_s": open_s,
    "read": n, "read_s": read_s, "per_s": n / read_s, "mb": size / 1e6,
}))
"""


# --- Orquestração --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    default_data = Path("/data") if Path("/data").is_dir() else Path("data")
    p = argparse.ArgumentParser(description="Benchmark de embeddings do Ariadne (Fase 0).")
    p.add_argument(
        "--data",
        type=Path,
        default=default_data,
        help="pasta com models/ (cache) e out/ (resultados); por omissão /data",
    )
    p.add_argument("--backends", default="ort,llamacpp", help="ort,llamacpp (http entra com --http-url)")
    p.add_argument("--variants", default="fp32,int8,f32,q8_0", help="ort: fp32,int8 · llamacpp: f32,q8_0")
    p.add_argument("--threads", help="ex.: 1,8,16 (por omissão: 1, metade e todas)")
    p.add_argument("--seconds", type=float, default=20.0, help="duração de cada medição")
    p.add_argument("--workloads", default="query,passage")
    p.add_argument("--passage-batch", type=int, default=16)
    p.add_argument(
        "--llama-server",
        default=shutil.which("llama-server"),
        help="binário do llama.cpp (vem na imagem completa)",
    )
    p.add_argument(
        "--gguf",
        action="append",
        default=[],
        metavar="VARIANTE=CAMINHO",
        help="usar um GGUF local em vez do descarregado (ex.: q8_0=/data/e5-q8_0.gguf)",
    )
    p.add_argument("--http-url", help="endpoint OpenAI-compatível a medir (plano B)")
    p.add_argument("--http-model", default="multilingual-e5-small")
    p.add_argument("--zim", type=Path, help="ZIM para testar a leitura com python-libzim")
    p.add_argument("--zim-articles", type=int, default=500)
    p.add_argument("--quick", action="store_true", help="teste rápido: 3 s por medição, 1 e todas as threads")
    return p.parse_args()  # fmt: skip


def thread_counts(args: argparse.Namespace, available: int) -> list[int]:
    if args.threads:
        return [int(t) for t in args.threads.split(",")]
    if args.quick:
        return sorted({1, available})
    return sorted({1, max(1, available // 2), available})


def main() -> int:
    args = parse_args()
    if args.quick:
        args.seconds = min(args.seconds, 3.0)
    data = args.data.resolve()
    models_dir, out_dir = data / "models", data / "out"
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now().astimezone()

    log(f"Ariadne bench v{BENCH_VERSION} · dados em {data}")
    system = system_info()
    log(f"CPU: {system['cpu']} · {system['threads_disponiveis']} threads · {system['mem_gb']} GB")

    log("Canário de bibliotecas nativas:")
    canaries: dict[str, dict[str, str]] = {}
    for name, code in CANARIES.items():
        status, out = run_isolated([sys.executable, "-c", code], timeout=180)
        canaries[name] = {"status": status, "detail": out.strip().splitlines()[-1] if out.strip() else ""}
        log(f"  {name:12} {status:20} {canaries[name]['detail'][:70]}")
    if args.llama_server:
        status, out = run_isolated([args.llama_server, "--version"], timeout=60)
        canaries["llama-server"] = {"status": status, "detail": " ".join(out.split())[:120]}
        log(f"  {'llama-server':12} {status:20} {canaries['llama-server']['detail'][:70]}")

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    plan = [key for key in VARIANTS if key[0] in backends and key[1] in variants]
    local_gguf = dict(item.split("=", 1) for item in args.gguf)
    skipped: list[dict[str, Any]] = []
    if any(b == "llamacpp" for b, _ in plan) and not args.llama_server:
        skipped += [
            {"backend": b, "variant": v, "status": "sem llama-server"} for b, v in plan if b == "llamacpp"
        ]
        plan = [key for key in plan if key[0] != "llamacpp"]

    log("Modelos:")
    tokenizer_path = None
    if any(b == "ort" for b, _ in plan):
        try:
            tokenizer_path = str(fetch(models_dir, E5_REPO, E5_REVISION, "tokenizer.json"))
        except (OSError, RuntimeError) as exc:
            log(f"  falhou o download do tokenizer: {exc}")
            skipped += [
                {"backend": b, "variant": v, "status": "sem modelo", "error": f"tokenizer: {exc}"}
                for b, v in plan
                if b == "ort"
            ]
            plan = [key for key in plan if key[0] != "ort"]
    model_paths: dict[tuple[str, str], str] = {}
    for key in list(plan):
        if key[0] == "ort":
            try:
                model_paths[key] = str(fetch(models_dir, E5_REPO, E5_REVISION, VARIANTS[key]))
            except (OSError, RuntimeError) as exc:
                log(f"  falhou o download de {VARIANTS[key]}: {exc}")
                plan.remove(key)
                skipped.append(
                    {"backend": key[0], "variant": key[1], "status": "sem modelo", "error": str(exc)}
                )
            continue
        found = local_gguf.get(key[1]) or next(
            (str(d / VARIANTS[key]) for d in (data / "gguf", *GGUF_DIRS) if (d / VARIANTS[key]).is_file()),
            None,
        )
        if found:
            model_paths[key] = found
        else:
            plan.remove(key)
            failed = next((str(d / "FALHOU.txt") for d in GGUF_DIRS if (d / "FALHOU.txt").is_file()), None)
            skipped.append({"backend": key[0], "variant": key[1], "status": "sem GGUF", "error": failed})
    for key, path in model_paths.items():
        log(f"  {key[0]}-{key[1]}: {path}")

    available = system["threads_disponiveis"] or 1
    threads = thread_counts(args, available)
    passages = build_passages()
    quality_texts = list(SANITY.values()) + QUERIES[:4] + passages[:3]
    configs: list[tuple[str, str, int]] = [(b, v, t) for b, v in plan for t in threads]
    if args.http_url:
        configs.append(("http", "remoto", 0))

    log(f"Medições: {len(configs)} configurações × {args.seconds:.0f} s por carga")
    results: list[dict[str, Any]] = []
    vectors: dict[str, list[list[float]]] = {}
    for backend, variant, n_threads in configs:
        name = f"{backend}-{variant}"
        spec = {
            "backend": backend, "variant": variant, "threads": n_threads,
            "seconds": args.seconds, "workloads": args.workloads.split(","),
            "passage_batch": args.passage_batch, "queries": QUERIES, "passages": passages,
            "quality_texts": quality_texts, "want_quality": name not in vectors,
            "model_path": model_paths.get((backend, variant)), "tokenizer_path": tokenizer_path,
            "llama_server": args.llama_server, "http_url": args.http_url, "http_model": args.http_model,
            "server_log": str(logs_dir / f"{name}-t{n_threads}.server.log"),
        }  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            spec_path, result_path = Path(tmp) / "spec.json", Path(tmp) / "result.json"
            spec_path.write_text(json.dumps(spec))
            timeout = 900 + 6 * args.seconds
            status, _ = run_isolated(
                [sys.executable, os.path.abspath(__file__), "_worker", str(spec_path), str(result_path)],
                timeout, logs_dir / f"{name}-t{n_threads}.log",
            )  # fmt: skip
            if status == "ok" and result_path.exists():
                result = json.loads(result_path.read_text())
            else:
                result = {"status": status, "error": f"ver {logs_dir / f'{name}-t{n_threads}.log'}"}
        if "quality_vectors" in result:
            vectors[name] = result.pop("quality_vectors")
        result.update(backend=backend, variant=variant, threads=n_threads or None)
        results.append(result)
        log("  " + format_row(result))

    quality = compute_quality(vectors)
    best = compute_best(results)
    zim = None
    if args.zim:
        status, out = run_isolated(
            [sys.executable, "-c", ZIM_CODE, str(args.zim), str(args.zim_articles)], 600
        )
        zim = {
            "status": status,
            **(json.loads(out.strip().splitlines()[-1]) if status == "ok" else {"detail": out[-500:]}),
        }

    report = {
        "bench_version": BENCH_VERSION,
        "started": started.isoformat(timespec="seconds"),
        "duration_s": round((datetime.now().astimezone() - started).total_seconds()),
        "system": system, "canaries": canaries,
        "args": {k: str(v) for k, v in vars(args).items()},
        "assets": {"e5": f"{E5_REPO}@{E5_REVISION}", **{f"{b}-{v}": p for (b, v), p in model_paths.items()}},
        "results": results + skipped, "quality": quality, "best": best, "libzim": zim,
    }  # fmt: skip
    stamp = started.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"ariadne-bench-{system['host'] or 'host'}-{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print_summary(report, report_path)
    return 0


def compute_quality(vectors: dict[str, list[list[float]]]) -> dict[str, Any]:
    ref_name = f"{REFERENCE[0]}-{REFERENCE[1]}"
    ref = vectors.get(ref_name) or next(iter(vectors.values()), None)
    ref_used = ref_name if ref_name in vectors else next(iter(vectors), None)
    quality: dict[str, Any] = {"referencia": ref_used}
    for name, vecs in vectors.items():
        entry: dict[str, Any] = {
            "sim_pt_en": cosine(vecs[0], vecs[1]),
            "sim_pt_outro": cosine(vecs[0], vecs[2]),
        }
        if ref is not None and len(ref[0]) == len(vecs[0]):
            sims = [cosine(a, b) for a, b in zip(vecs, ref, strict=True)]
            entry["cos_vs_ref_min"] = min(sims)
            entry["cos_vs_ref_mean"] = statistics.mean(sims)
        elif ref is not None:
            entry["nota"] = f"dimensão {len(vecs[0])} ≠ referência {len(ref[0])}"
        quality[name] = entry
    return quality


def compute_best(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if r.get("status") == "ok"]
    best: dict[str, Any] = {}
    passage = [r for r in ok if r.get("passage")]
    if passage:
        top = max(passage, key=lambda r: r["passage"]["sentences_per_s"])
        sps = top["passage"]["sentences_per_s"]
        best["passage"] = {
            "config": label(top),
            "sentences_per_s": sps,
            "budget_minutes": DAILY_BUDGET / sps / 60,
            "chunks_per_day_max": int(sps * 86400),
        }
    query = [r for r in ok if r.get("query")]
    if query:
        top = min(query, key=lambda r: r["query"]["p50_ms"])
        best["query"] = {"config": label(top), "p50_ms": top["query"]["p50_ms"]}
    return best


def label(result: dict[str, Any]) -> str:
    threads = f" {result['threads']}t" if result.get("threads") else ""
    return f"{result['backend']}-{result['variant']}{threads}"


def fmt(value: float | None, spec: str) -> str:
    return "—" if value is None else format(value, spec)


def format_row(r: dict[str, Any]) -> str:
    query, passage = r.get("query") or {}, r.get("passage") or {}
    return (
        f"{r['backend']:9} {r['variant']:7} {fmt(r.get('threads'), 'd'):>3}t "
        f"{r.get('status', '?'):20} "
        f"consulta p50 {fmt(query.get('p50_ms'), '7.1f')} ms  p95 {fmt(query.get('p95_ms'), '7.1f')} ms  "
        f"passagens {fmt(passage.get('sentences_per_s'), '7.2f')}/s  "
        f"tokens {fmt(passage.get('tokens_per_s'), '8.0f')}/s"
        + (f"  [{r['error']}]" if r.get("error") else "")
    )


def format_skipped(r: dict[str, Any]) -> str:
    return f"{r['backend']}-{r['variant']}: {r['status']}" + (f" ({r['error']})" if r.get("error") else "")


def print_summary(report: dict[str, Any], path: Path) -> None:
    system = report["system"]
    flags = " · ".join(f"{k} {'sim' if v else 'NÃO'}" for k, v in system["flags"].items()) or "n/d"
    lines = [
        "",
        "=== RESUMO ARIADNE-BENCH (copiar daqui até FIM) ===",
        f"máquina: {system['cpu']} · {system['threads_disponiveis']} threads · "
        f"NUMA {system['numa_nodes']} · {system['mem_gb']} GB · {system['machine']}",
        f"flags: {flags}",
        "canário: " + " · ".join(f"{k} {v['status']}" for k, v in report["canaries"].items()),
        "medições:",
        *("  " + (format_row(r) if "threads" in r else format_skipped(r)) for r in report["results"]),
        f"qualidade (referência {report['quality'].get('referencia')}):",
    ]  # fmt: skip
    for name, q in report["quality"].items():
        if name == "referencia":
            continue
        lines.append(
            f"  {name:14} cos_vs_ref min {fmt(q.get('cos_vs_ref_min'), '.4f')} "
            f"média {fmt(q.get('cos_vs_ref_mean'), '.4f')} · "
            f"pt↔en {q['sim_pt_en']:.3f} vs pt↔outro {q['sim_pt_outro']:.3f}"
        )
    best = report["best"]
    if "passage" in best:
        b = best["passage"]
        lines.append(
            f"melhor passagem: {b['config']} → {b['sentences_per_s']:.2f}/s · "
            f"{DAILY_BUDGET} chunks em {b['budget_minutes']:.1f} min · máx. {b['chunks_per_day_max']}/dia"
        )
    if "query" in best:
        lines.append(f"melhor consulta: {best['query']['config']} → p50 {best['query']['p50_ms']:.1f} ms")
    if report["libzim"]:
        z = report["libzim"]
        if z["status"] == "ok":
            lines.append(
                f"libzim: {z['articles']} artigos · leitura {z['per_s']:.0f} entradas/s · "
                f"índice full-text {z['fulltext_index']}"
            )
        else:
            lines.append(f"libzim: {z['status']}")
    lines += [f"duração: {report['duration_s']} s · relatório: {path}", "=== FIM ==="]
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_worker":
        worker_main(sys.argv[2], sys.argv[3])
        sys.exit(0)
    sys.exit(main())
