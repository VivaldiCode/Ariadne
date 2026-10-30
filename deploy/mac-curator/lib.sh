# Funções partilhadas pelos scripts (source). Sem segredos: a chave só se lê do ficheiro e nunca se imprime.
set -euo pipefail

LABEL="org.ariadne.curator"
APP_DIR="${ARIADNE_CURATOR_DIR:-$HOME/Library/Application Support/ariadne-curator}"
LOG_DIR="${ARIADNE_CURATOR_LOGS:-$HOME/Library/Logs/ariadne-curator}"
KEY_FILE="$APP_DIR/api-key"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '%s\n' "$*"; }
die() { printf 'Erro: %s\n' "$*" >&2; exit 1; }

load_lock() {
  # shellcheck disable=SC1091
  source "$HERE/model.lock"
  MODEL_PATH="$APP_DIR/models/$MODEL_FILE"
  MODEL_URL="https://huggingface.co/$MODEL_REPO/resolve/$MODEL_REVISION/$MODEL_FILE"
}

llama_server_bin() {
  command -v llama-server 2>/dev/null || { [ -x /opt/homebrew/bin/llama-server ] && echo /opt/homebrew/bin/llama-server; } \
    || die "llama-server is not installed (brew install llama.cpp)."
}

sha_ok() { # sha_ok ficheiro
  [ -f "$1" ] && [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$MODEL_SHA256" ]
}

fetch_model() {
  # Descarrega com retoma (-C -) para .part e só promove a ficheiro final depois do sha256 bater.
  mkdir -p "$APP_DIR/models"
  if [ -f "$MODEL_PATH" ] && [ "$(stat -f %z "$MODEL_PATH")" = "$MODEL_BYTES" ]; then
    say "Verifying the already downloaded model (sha256)…"
    if sha_ok "$MODEL_PATH"; then say "Model ok: $MODEL_FILE"; return; fi
    say "The file does not match model.lock: downloading again."
    rm -f "$MODEL_PATH"
  fi
  say "Downloading $MODEL_FILE ($(( MODEL_BYTES / 1000000 )) MB) from huggingface.co/$MODEL_REPO …"
  curl -fL --retry 5 --retry-delay 5 -C - -o "$MODEL_PATH.part" "$MODEL_URL"
  [ "$(stat -f %z "$MODEL_PATH.part")" = "$MODEL_BYTES" ] || die "unexpected size; run the script again (it resumes)."
  say "Verifying sha256…"
  sha_ok "$MODEL_PATH.part" || { rm -f "$MODEL_PATH.part"; die "the download's sha256 does not match model.lock."; }
  mv "$MODEL_PATH.part" "$MODEL_PATH"
  say "Model ok: $MODEL_FILE"
}

ensure_key() {
  mkdir -p "$APP_DIR"
  chmod 700 "$APP_DIR"
  if [ ! -s "$KEY_FILE" ]; then
    ( umask 077; openssl rand -hex 24 > "$KEY_FILE" )
    say "API key created in $KEY_FILE (mode 600). Copy it with ./copy-key.sh; it is never printed."
  fi
  chmod 600 "$KEY_FILE"
}

lan_ip() { ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "?"; }

bind_port() { # lê PORT de config.env (omissão 8085)
  local PORT=8085
  # shellcheck disable=SC1091
  [ -f "$APP_DIR/config.env" ] && source "$APP_DIR/config.env"
  echo "$PORT"
}

wait_health() { # wait_health porta segundos
  local i
  for ((i = 0; i < $2; i++)); do
    if curl -fs -m 2 "http://localhost:$1/health" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}
