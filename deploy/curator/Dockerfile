# O curador do standalone (perfil `llm` do docker-compose.yml): o llama-server do llama.cpp só em
# CPU, com um modelo pequeno fixado (model.lock). Com uma GPU noutra máquina usa-se essa (CURATOR_URL).
#
# Hardware (ARCHITECTURE.md, secção 9.3): compila-se sem nenhuma extensão que o Nehalem não tenha
# (GGML_NATIVE=OFF e AVX/AVX2/FMA/F16C/BMI2 desligados à mão), as mesmas flags e a mesma versão do
# bench (bench/embeddings/Dockerfile), e o build falha se alguma unidade sair com elas.

FROM debian:bookworm-slim AS build
ARG LLAMACPP_REF=b11854
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${LLAMACPP_REF}" https://github.com/ggml-org/llama.cpp /llama.cpp
WORKDIR /llama.cpp
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
      -DBUILD_SHARED_LIBS=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_BUILD_TESTS=OFF \
      -DGGML_NATIVE=OFF -DGGML_AVX=OFF -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_F16C=OFF \
      -DGGML_BMI2=OFF -DGGML_AVX512=OFF -DGGML_AVX_VNNI=OFF -DGGML_SSE42=ON
RUN if grep -Eo -- '-m(avx[a-z0-9_]*|fma|f16c|bmi2|amx[a-z0-9_-]*)\b|-march=native' build/compile_commands.json \
        | sort -u | grep . ; then echo "ERRO: flags proibidas no build do llama.cpp" >&2; exit 1; fi
RUN cmake --build build --target llama-server -j"$(nproc)" && strip build/bin/llama-server

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /llama.cpp/build/bin/llama-server /usr/local/bin/llama-server
COPY model.lock entrypoint.sh /usr/local/share/ariadne-curator/
RUN llama-server --version \
 && useradd --uid 1000 --home-dir /models curator \
 && mkdir -p /models && chown curator:curator /models   # o volume herda o dono
USER curator
EXPOSE 8085
# /health dá 503 enquanto o modelo carrega (e não responde durante a 1.ª descarga): ver start_period.
HEALTHCHECK --interval=15s --timeout=5s --start-period=1800s --retries=3 \
    CMD curl -fsS -o /dev/null "http://$(hostname -i | cut -d' ' -f1):8085/health" || exit 1
ENTRYPOINT ["/usr/local/share/ariadne-curator/entrypoint.sh"]
