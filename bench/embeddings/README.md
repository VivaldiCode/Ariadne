# Embedding benchmark (Phase 0)

It answers the question in section 9.3 of [ARCHITECTURE.md](../../ARCHITECTURE.md): **can the server (2× Xeon X5550, without AVX/AVX2/FMA/F16C/BMI2) generate embeddings, and at what rate?**

Model: `intfloat/multilingual-e5-small` (384 dimensions), pinned revision `614241f`.

| Backend | Variants | Where it comes from |
|---|---|---|
| `ort` (onnxruntime) | `fp32`, `int8` | official ONNX from the model's repository |
| `llamacpp` (`llama-server`) | `f32`, `q8_0` | llama.cpp `b11854` built **without** AVX/AVX2/FMA/F16C/BMI2 (the build fails if any of those flags appears); GGUF converted in the image with the official converter ([`convert_e5.py`](convert_e5.py)) |
| `http` | — | any endpoint compatible with the OpenAI API, only with `--http-url` (plan B: embeddings delegated to another machine) |

The benchmark measures:

- **Canary:** imports numpy, onnxruntime, tokenizers, libzim and `llama-server`, each in a separate process. An `ILLEGAL_INSTRUCTION` (SIGILL) is recorded and the benchmark continues.
- **Query:** one short sentence at a time (~16 tokens), with p50/p95 latency. This is the router's case (section 5).
- **Passage:** batches of 16 passages of ~256 tokens (the size of a chunk), in sentences/s and tokens/s. This is the `indexer` worker's case.
- **Threads:** 1, half and all (on the X5550: 1, 8 and 16).
- **Quality:** cosine of each variant against ORT fp32 on the same texts, and a multilingual check («Carnation Revolution» ↔ «Revolução dos Cravos» against an unrelated text).
- **Extrapolation:** how long the 20,000 chunks/day of section 6.4 take with the best configuration.
- **libzim** (optional, `--zim`): reads random entries from a ZIM with `python-libzim`.

## Run on the data server

In the shell of the data server (*System → Shell* or `ssh`), inside a dataset (replace `<pool>` with yours):

```bash
cd /mnt/<pool>
git clone https://github.com/VivaldiCode/Ariadne.git ariadne && cd ariadne
```

Without `git`, `curl -L https://github.com/VivaldiCode/Ariadne/archive/refs/heads/main.tar.gz | tar xz` also works.

**1. Quick test, ORT only** (~2 min build, ~1 min run). It answers the main question right away:

```bash
sudo docker build --target ort -t ariadne-bench:ort bench/embeddings
sudo docker run --rm -v /mnt/<pool>/ariadne-bench:/data ariadne-bench:ort --quick --backends ort
```

**2. Full benchmark** (the build compiles llama.cpp and converts the model, 20–40 min on the X5550; the run takes ~15 min):

```bash
sudo docker build -t ariadne-bench bench/embeddings
sudo docker run --rm -v /mnt/<pool>/ariadne-bench:/data ariadne-bench
```

Optional: add a real ZIM to measure `python-libzim`:

```bash
sudo docker run --rm -v /mnt/<pool>/ariadne-bench:/data \
  -v /mnt/<pool>/<zim-folder>:/zim:ro \
  ariadne-bench --zim /zim/<file>.zim
```

The ONNX models (~600 MB) are cached in `/mnt/<pool>/ariadne-bench/models`, and the results in `/mnt/<pool>/ariadne-bench/out`.

## What to send back

1. **The end-of-run block**, from `=== RESUMO ARIADNE-BENCH` to `=== FIM ===` (from both runs, if you do both).
2. **The JSON file** named on the last line: `out/ariadne-bench-<host>-<date>.json`.
3. Only if any line says `ILLEGAL_INSTRUCTION`, `erro` or `sem GGUF`:
   - the log of that configuration in `out/logs/` (e.g. `llamacpp-q8_0-t16.log` and `.server.log`);
   - for `sem GGUF`, the conversion log: `sudo docker run --rm --entrypoint cat ariadne-bench /opt/gguf/conversao.log | tail -50`.
4. If the **build** fails: the last ~50 lines of `docker build` and the output of `lscpu`.
5. **Server canary** (the native dependencies of `ariadne-server` must also run without AVX), from the repository root:

   ```bash
   sudo docker compose build server
   sudo docker compose run --rm --no-deps --entrypoint python server -c "import pydantic_core, psycopg, uvloop, httptools, libzim.reader, rpds, _cffi_backend, mcp, numpy, onnxruntime, tokenizers, pyarrow.parquet; from cryptography.hazmat.primitives import hashes; h = hashes.Hash(hashes.SHA256()); h.update(b'x'); print('ok')"
   ```

6. **Phase 10 image canary** (Moodle with PHP and Jobe run on the data server/hypervisor, on the same X5550 without AVX; they were only verified on a Mac). The Moodle PHP and the Jobe Node/Python must start and execute code:

   ```bash
   sudo docker compose build moodle
   sudo docker compose run --rm --no-deps --entrypoint php moodle -r 'foreach (["pgsql","gd","intl","mbstring","zip","xml","sodium","Zend OPcache"] as $e) { if (!extension_loaded($e)) { echo "missing $e\n"; exit(1); } } echo "ok\n";'
   # Jobe (amd64 image) and the Node inside it, which is the part with JIT and the one most at risk of SIGILL:
   sudo docker compose up -d jobe jobe-gateway
   curl -s -X POST -H 'Content-Type: application/json' http://localhost:8804/jobe/index.php/restapi/runs -d '{"run_spec":{"language_id":"nodejs","sourcefilename":"p.js","sourcecode":"console.log(6*7)","parameters":{"memorylimit":4096}}}'
   ```

   Expected: `ok` and `"stdout":"42\n"` with `"outcome":15`. An `outcome` of 12 with `Illegal instruction` in `stderr` rules out Node in that image (the CodeRunner JavaScript exercises need it); Python, C and Java are not affected.

7. **Phase 11 image canary** (Calibre and Kavita run on the data server, on the same X5550 without AVX; they were only verified on a Mac, on arm64). Calibre is the official x86_64 binary (Qt 6 and its Python, no GUI) and Kavita is .NET: both must start and do their work:

   ```bash
   sudo docker compose build calibre
   # ebook-convert converts HTML to EPUB (starts Qt in offscreen mode) and calibredb talks to the library:
   sudo docker compose run --rm --no-deps --entrypoint sh calibre -c 'calibredb --version && echo "<p>hello</p>" > /tmp/a.html && ebook-convert /tmp/a.html /tmp/a.epub > /dev/null && echo ok'
   sudo docker compose up -d calibre kavita
   curl -s http://localhost:8807/healthz; curl -s http://localhost:8806/api/health
   ```

   Expected: `ok`, `{"status": "ok", "calibre": "calibredb (calibre 9.16)"}` and `Healthy`. An `Illegal instruction` in `ebook-convert` rules out that Calibre image (the alternative is Debian's `calibre` package, `apt install calibre-bin`, older but built for generic x86-64); Kavita only uses the .NET CLR, which chooses instructions at run time. The server's book extractor (EPUB via zip + bs4, PDF via `pypdf`) is pure Python and needs no canary.

With these results, the `EMBEDDER` (onnx, llamacpp or http) is chosen and it is decided whether the daily budget of 20,000 chunks is realistic.

## How to read the summary

| Field | Meaning |
|---|---|
| `canário` (canary) | `ok` or `ILLEGAL_INSTRUCTION` per library; a SIGILL here rules that library out on the server |
| `consulta p50/p95` (query) | embedding latency for one question (the router's target: tens of ms) |
| `passagens/s`, `tokens/s` | chunk indexing rate |
| `cos_vs_ref` | fidelity against ORT fp32 (≥ 0.99 is equivalent; below ~0.98, suspect the conversion or the quantization) |
| `pt↔en vs pt↔outro` (pt↔en vs pt↔other) | the same fact in two languages should be closer than an unrelated text (with e5, similarities all lie between ~0.7 and 1.0, so the difference is what matters) |
| `melhor passagem` (best passage) | how long 20,000 chunks/day take and the theoretical daily maximum |

## If something fails

- **ORT with `ILLEGAL_INSTRUCTION`:** try an older onnxruntime version:
  `sudo docker build --target ort --build-arg ORT_VERSION=1.20.1 -t ariadne-bench:ort bench/embeddings`.
- **`sem GGUF` (no GGUF)** (the conversion needs PyTorch, which may not run on this CPU): see `conversao.log`. If it was a network failure, repeat only the conversion with `sudo docker build --no-cache-filter gguf -t ariadne-bench bench/embeddings`. Otherwise, convert on the Mac and copy the files. The GGUF does not depend on the CPU.

  ```bash
  # on the Mac, at the repository root
  docker build --target gguf-export --output type=local,dest=./gguf bench/embeddings
  scp gguf/*.gguf <your-server>:/mnt/<pool>/ariadne-bench/gguf/
  ```

  The benchmark looks for GGUF files in `/data/gguf` before `/opt/gguf`. It also accepts `--gguf q8_0=/data/other.gguf`.
- **Plan B (HTTP):** with `llama-server --embeddings --pooling mean` on another machine, measure the latency over the network:
  `... ariadne-bench --backends none --http-url http://<host>:8080`.

## Options

```text
--quick                 3 s per measurement, 1 and all threads
--backends ort,llamacpp --variants fp32,int8,f32,q8_0
--threads 1,8,16        --seconds 20        --passage-batch 16
--zim FILE              --zim-articles 500
--http-url URL          --http-model NAME
--gguf VARIANT=PATH     --data /data
```

## Development (Mac)

```bash
cd bench/embeddings
uv run bench.py --quick --backends ort      # uses ./data as /data
docker build -t ariadne-bench . && docker run --rm -v "$PWD/data:/data" ariadne-bench --quick
```

The main process only uses the standard library. Everything native runs in subprocesses (`bench.py _worker …`), so that a SIGILL is measured instead of terminating the benchmark.
