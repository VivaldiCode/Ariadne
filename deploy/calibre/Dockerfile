# Calibre só em linha de comandos (calibredb, ebook-convert) atrás de um agente HTTP com chave de API
# (STRUCTURE.md, 6.3). Imagem fina: o Python da imagem corre o agente; o Calibre é o binário oficial
# (com a sua própria runtime), verificado por sha256, com as bibliotecas de sistema que o Qt pede.
# O Calibre não usa GUI aqui, mas o Qt arranca no modo `offscreen`.
# A versão e os hashes estão fixos; para mudar de versão: descarregar os dois .txz e atualizar os hashes.
FROM python:3.12-slim-bookworm

ARG CALIBRE_VERSION=9.16.0
ARG CALIBRE_SHA256_X86_64=03cd4fda43de23d65405a3c90b924bc4a5c84753e388589d4ca343d9ba561a1d
ARG CALIBRE_SHA256_ARM64=82bf5de819a655fc0c8b789d388fef53c4583f9c2f9b83a14df42724519a197d
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils \
      libegl1 libopengl0 libgl1 libxcb-cursor0 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 \
      libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxi6 libasound2 libdbus-1-3 \
      libglib2.0-0 libxkbfile1 \
 && rm -rf /var/lib/apt/lists/*

RUN case "$TARGETARCH" in \
      amd64) arch=x86_64; sum="$CALIBRE_SHA256_X86_64" ;; \
      arm64) arch=arm64; sum="$CALIBRE_SHA256_ARM64" ;; \
      *) echo "arquitetura não suportada: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /tmp/calibre.txz \
      "https://download.calibre-ebook.com/${CALIBRE_VERSION}/calibre-${CALIBRE_VERSION}-${arch}.txz" \
 && echo "${sum}  /tmp/calibre.txz" | sha256sum -c - \
 && mkdir /opt/calibre && tar -xJf /tmp/calibre.txz -C /opt/calibre \
 && rm /tmp/calibre.txz

ENV PATH="/opt/calibre:$PATH" \
    QT_QPA_PLATFORM=offscreen \
    HOME=/tmp/calibre-home \
    CALIBRE_CONFIG_DIRECTORY=/tmp/calibre-config \
    CALIBRE_CACHE_DIRECTORY=/tmp/calibre-cache \
    CALIBRE_LIBRARY=/library

COPY agent.py /opt/ariadne-calibre/agent.py

RUN useradd --uid 1000 --home-dir /tmp/calibre-home calibre \
 && mkdir -p /library && chown calibre /library
USER calibre
WORKDIR /library
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=4)"]
CMD ["python", "/opt/ariadne-calibre/agent.py"]
