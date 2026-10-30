#!/bin/bash
# O que o launchd corre: o llama-server com o modelo fixado, em Metal, só com chave de API.
# Instalado em «~/Library/Application Support/ariadne-curator/serve.sh» pelo install.sh.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$APP_DIR/model.lock"

# Opções que se afinam em config.env (nada de segredos): PORT, CTX, THREADS, BIND.
PORT=8085
BIND=              # vazio = só neste Mac (o llama-server escuta em localhost); para a LAN, define BIND em config.env
CTX=32768          # 40 000 caracteres de artigo (~12 mil tokens) + a reescrita cabem à vontade
THREADS=8          # só a parte em CPU; o resto corre na GPU (Metal)
# shellcheck disable=SC1091
[ -f "$APP_DIR/config.env" ] && source "$APP_DIR/config.env"

MODEL="$APP_DIR/models/$MODEL_FILE"
[ -f "$MODEL" ] || { echo "Model $MODEL is missing (run install.sh/update.sh)" >&2; exit 78; }
[ "$(stat -f %z "$MODEL")" = "$MODEL_BYTES" ] || { echo "Unexpected model size (run update.sh)" >&2; exit 78; }
[ -s "$APP_DIR/api-key" ] || { echo "API key is missing at $APP_DIR/api-key" >&2; exit 78; }

LLAMA="$(command -v llama-server || echo /opt/homebrew/bin/llama-server)"
exec "$LLAMA" \
  --model "$MODEL" \
  --alias "$MODEL_ALIAS" \
  ${BIND:+--host "$BIND"} --port "$PORT" \
  --api-key-file "$APP_DIR/api-key" \
  --ctx-size "$CTX" --parallel 1 \
  --n-gpu-layers 99 --flash-attn on --threads "$THREADS" \
  --no-mmproj --no-webui --jinja
