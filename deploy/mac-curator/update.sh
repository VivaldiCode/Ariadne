#!/bin/bash
# Atualiza o curador: llama.cpp (brew), o modelo do model.lock (se mudou) e o serviço.
#   ./update.sh            só o que o model.lock/serve.sh pedem
#   ./update.sh --brew     também faz «brew upgrade llama.cpp» antes
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source ./lib.sh
load_lock

if [ "${1:-}" = "--brew" ]; then
  brew update >/dev/null && brew upgrade llama.cpp || true
fi
say "llama-server: $("$(llama_server_bin)" --version 2>&1 | head -1)"
fetch_model
install -m 755 serve.sh "$APP_DIR/serve.sh"
install -m 644 model.lock "$APP_DIR/model.lock"

# Modelos antigos (de um model.lock anterior) ocupam disco: lista-os, não os apaga sozinho.
for old in "$APP_DIR"/models/*.gguf; do
  [ "$old" = "$MODEL_PATH" ] || say "Old model (you can delete it): $old"
done

DOMAIN="gui/$(id -u)"
[ -f "/Library/LaunchDaemons/$LABEL.plist" ] && DOMAIN="system"
launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || say "The service is not loaded: run ./install.sh"
PORT="$(bind_port)"
wait_health "$PORT" 180 && say "Server ready." || { say "Did not respond; check $LOG_DIR/llama-server.log"; exit 1; }
./check.sh
