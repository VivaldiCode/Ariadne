# Jobe do Ariadne: a imagem oficial (fixa pelo digest) mais o iptables, para cortar a rede ao código dos alunos
# (ver deploy/jobe/entrypoint.sh). Contexto de build: este diretório.
FROM trampgeek/jobeinabox@sha256:b442c8e63f016ff33aa2b6f28c36abc8ebd30cbb39e8e025cadeec2107c5ce85

RUN apt-get update \
    && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/ariadne-jobe-entrypoint
RUN chmod +x /usr/local/bin/ariadne-jobe-entrypoint
