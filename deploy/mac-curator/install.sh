#!/bin/bash
# Instala o curador no Mac: modelo (verificado), chave de API, serviço launchd. Idempotente.
#   ./install.sh            LaunchAgent (arranca quando o utilizador inicia sessão)
#   sudo ./install.sh --daemon   LaunchDaemon (arranca com o Mac, antes de haver sessão)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source ./lib.sh

DAEMON=0
[ "${1:-}" = "--daemon" ] && DAEMON=1
if [ "$DAEMON" = 1 ] && [ "$(id -u)" != 0 ]; then die "--daemon needs sudo."; fi
[ "$(uname -m)" = "arm64" ] || die "this is for Apple Silicon (Metal)."
if [ "$DAEMON" = 1 ]; then
  OWNER="${SUDO_USER:?corre com sudo a partir da tua conta}"
  HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
  export HOME
  APP_DIR="$HOME/Library/Application Support/ariadne-curator"; LOG_DIR="$HOME/Library/Logs/ariadne-curator"
  KEY_FILE="$APP_DIR/api-key"
fi

load_lock
BIN="$(llama_server_bin)"
say "llama-server: $("$BIN" --version 2>&1 | head -1) ($BIN)"
mkdir -p "$APP_DIR" "$LOG_DIR"
[ "$DAEMON" = 1 ] && chown -R "$OWNER" "$APP_DIR" "$LOG_DIR"

as_owner() { if [ "$DAEMON" = 1 ]; then sudo -u "$OWNER" -H env HOME="$HOME" "$@"; else "$@"; fi; }

# 1. modelo e chave (como o dono, não como root)
if [ "$DAEMON" = 1 ]; then
  as_owner bash -c "cd '$HERE' && source ./lib.sh && ARIADNE_CURATOR_DIR='$APP_DIR' load_lock && fetch_model && ensure_key"
else
  fetch_model
  ensure_key
fi

# 2. os ficheiros que o serviço lê (a cópia no APP_DIR é a que corre: o repositório pode mudar de sítio)
install -m 755 serve.sh "$APP_DIR/serve.sh"
install -m 644 model.lock "$APP_DIR/model.lock"
[ -f "$APP_DIR/config.env" ] || printf '# PORT=8085\n# CTX=32768\n# THREADS=8\n# BIND=   (vazio: só neste Mac; para a LAN, o endereço onde escutar)\n' > "$APP_DIR/config.env"
[ "$DAEMON" = 1 ] && chown -R "$OWNER" "$APP_DIR"

# 3. o plist
if [ "$DAEMON" = 1 ]; then
  PLIST="/Library/LaunchDaemons/$LABEL.plist"; DOMAIN="system"
  USER_XML="  <key>UserName</key>
  <string>$OWNER</string>
"
else
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"; DOMAIN="gui/$(id -u)"; USER_XML=""
  mkdir -p "$HOME/Library/LaunchAgents"
fi
python3 - "$PLIST" "$LABEL" "$APP_DIR" "$LOG_DIR" "$USER_XML" <<'PY'
import sys
plist, label, app, logs, user = sys.argv[1:6]
text = open("ai.plist.template").read()
for key, value in {"@LABEL@": label, "@APP_DIR@": app, "@LOG_DIR@": logs, "@DAEMON_USER@": user}.items():
    text = text.replace(key, value)
open(plist, "w").write(text)
PY
plutil -lint "$PLIST" >/dev/null
[ "$DAEMON" = 1 ] && chown root:wheel "$PLIST" && chmod 644 "$PLIST"

# 4. (re)carrega o serviço
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

PORT="$(bind_port)"
say "Waiting for the model to load (may take 1–2 minutes)…"
if wait_health "$PORT" 180; then
  say "Server ready."
else
  say "Not responding yet; check the log: tail -f '$LOG_DIR/llama-server.log'"
  exit 1
fi

./check.sh || true
cat <<TXT

Installed. Ariadne side still missing (dashboard → Integrations → Curator):
  CURATOR_URL      http://$(lan_ip):$PORT      (the Mac's IP on the LAN; a DHCP reservation is advised)
  CURATOR_MODEL    $MODEL_ALIAS
  CURATOR_API_KEY  ./copy-key.sh  (copies the key to the clipboard, without showing it)
  and the Mac's IP in «Allowed hosts» (PRIVATE_ALLOWED_HOSTS), on the same screen.
TXT
