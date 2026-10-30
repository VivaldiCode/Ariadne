#!/bin/sh
# O curador do standalone: descarrega o modelo fixado (model.lock) para o volume na 1.ª vez,
# confirma o tamanho e o sha256, e arranca o llama-server só com chave de API.
set -eu
. /usr/local/share/ariadne-curator/model.lock

: "${CURATOR_API_KEY:?defina CURATOR_API_KEY no .env (openssl rand -hex 24)}"
MODEL="/models/$MODEL_FILE"
if [ ! -f "$MODEL.ok" ]; then
  if [ ! -f "$MODEL" ] || [ "$(stat -c %s "$MODEL")" != "$MODEL_BYTES" ]; then
    # CURATOR_MODEL_URL: um espelho (ou um servidor da LAN, para instalações offline) com o mesmo
    # ficheiro; o sha256 de baixo confere-o na mesma. Retoma uma descarga interrompida (.part).
    URL="${CURATOR_MODEL_URL:-https://huggingface.co/$MODEL_REPO/resolve/$MODEL_REVISION/$MODEL_FILE}"
    echo "Downloading $MODEL_FILE ($MODEL_BYTES bytes) from $URL to /models..."
    curl -fL --retry 5 --retry-all-errors --retry-delay 10 -C - -o "$MODEL.part" "$URL"
    mv "$MODEL.part" "$MODEL"
  fi
  echo "$MODEL_SHA256  $MODEL" | sha256sum -c - || { rm -f "$MODEL"; echo "wrong sha256" >&2; exit 78; }
  touch "$MODEL.ok"
fi

# A chave vai pelo ambiente (LLAMA_API_KEY), não pela linha de comandos (ps).
export LLAMA_API_KEY="$CURATOR_API_KEY"
unset CURATOR_API_KEY
# Escuta no IP do contentor na rede do compose (calculado aqui: nada de endereços escritos à mão).
THREADS="${CURATOR_THREADS:-0}"
[ "$THREADS" = 0 ] && THREADS="$(nproc)"
exec llama-server \
  --model "$MODEL" \
  --alias "$MODEL_ALIAS" \
  --host "$(hostname -i | cut -d' ' -f1)" --port 8085 \
  --ctx-size "${CURATOR_CTX:-16384}" --parallel 1 \
  --threads "$THREADS" \
  --no-webui --jinja
