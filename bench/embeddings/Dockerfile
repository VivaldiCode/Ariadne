# Benchmark de embeddings do Ariadne (Fase 0). Ver README.md.
#   docker build -t ariadne-bench .                 # completo (ORT + llama.cpp)
#   docker build --target ort -t ariadne-bench .    # só ORT, build rápido
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --- Código do llama.cpp (partilhado pela compilação e pela conversão) ---------------------
FROM debian:bookworm-slim AS llamacpp-src
ARG LLAMACPP_REF=b11854
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${LLAMACPP_REF}" https://github.com/ggml-org/llama.cpp /llama.cpp

# --- llama.cpp sem AVX/AVX2/FMA/F16C/BMI2 (ARCHITECTURE.md, secção 9.3) -------------------
FROM llamacpp-src AS llamacpp
WORKDIR /llama.cpp
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
      -DBUILD_SHARED_LIBS=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_BUILD_TESTS=OFF \
      -DGGML_NATIVE=OFF -DGGML_AVX=OFF -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_F16C=OFF \
      -DGGML_BMI2=OFF -DGGML_AVX512=OFF -DGGML_AVX_VNNI=OFF -DGGML_SSE42=ON
# Falha o build se alguma unidade for compilada com extensões que o Nehalem não tem.
RUN if grep -Eo -- '-m(avx[a-z0-9_]*|fma|f16c|bmi2|amx[a-z0-9_-]*)\b|-march=native' build/compile_commands.json \
        | sort -u | grep . ; then echo "ERRO: flags proibidas no build do llama.cpp" >&2; exit 1; fi
RUN cmake --build build --target llama-server -j"$(nproc)" && strip build/bin/llama-server

# --- GGUF do e5-small, convertido com o conversor oficial (precisa de torch) ----------------
# Tolerante a falhas: se o torch rebentar neste CPU, a imagem fica sem GGUF e o bench diz-o.
# Uma falha também fica em cache: para repetir só esta etapa, --no-cache-filter gguf.
# No Mac: docker build --target gguf-export --output type=local,dest=./gguf .
FROM ${PYTHON_IMAGE} AS gguf
ARG E5_REVISION=614241f622f53c4eeff9890bdc4f31cfecc418b3
COPY --from=llamacpp-src /llama.cpp/convert_hf_to_gguf.py /llama.cpp/
COPY --from=llamacpp-src /llama.cpp/conversion /llama.cpp/conversion
COPY --from=llamacpp-src /llama.cpp/gguf-py /llama.cpp/gguf-py
COPY --from=llamacpp-src /llama.cpp/requirements /llama.cpp/requirements
COPY convert_e5.py /convert_e5.py
RUN set -u; mkdir -p /gguf /e5; cd /e5; \
    ( set -e; \
      for f in config.json model.safetensors modules.json 1_Pooling/config.json tokenizer.json \
               tokenizer_config.json special_tokens_map.json sentencepiece.bpe.model; do \
        mkdir -p "$(dirname "$f")"; echo "a descarregar $f"; \
        python -c "import shutil, sys, urllib.request; r = urllib.request.urlopen(sys.argv[1], timeout=120); shutil.copyfileobj(r, open(sys.argv[2], 'wb'))" \
          "https://huggingface.co/intfloat/multilingual-e5-small/resolve/${E5_REVISION}/$f" "$f"; \
      done; \
      pip install --no-cache-dir -r /llama.cpp/requirements/requirements-convert_hf_to_gguf.txt; \
      for t in f32 q8_0; do \
        python /convert_e5.py /llama.cpp /e5 --outtype "$t" --outfile "/gguf/multilingual-e5-small-$t.gguf"; \
      done ) > /gguf/conversao.log 2>&1 \
      || { echo "conversão GGUF falhou (rc=$?); ver conversao.log" > /gguf/FALHOU.txt; rm -f /gguf/*.gguf; }; \
    rm -rf /e5 /root/.cache

FROM scratch AS gguf-export
COPY --from=gguf /gguf/ /

# --- Bench só com ORT ---------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS ort
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
# Para testar outra versão se a atual rebentar: --build-arg ORT_VERSION=1.20.1
ARG ORT_VERSION=
RUN if [ -n "$ORT_VERSION" ]; then uv pip install --python /app/.venv/bin/python "onnxruntime==$ORT_VERSION"; fi
COPY bench.py ./
VOLUME /data
ENTRYPOINT ["python", "bench.py"]

# --- Completo: ORT + llama.cpp + GGUF -----------------------------------------------------
FROM ort AS full
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=llamacpp /llama.cpp/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=gguf /gguf/ /opt/gguf/
