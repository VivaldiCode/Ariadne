#!/bin/bash
# Pára e remove o serviço. Mantém o modelo e a chave (apaga-os à mão se quiseres: KEEP=0).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source ./lib.sh
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
if [ -f "/Library/LaunchDaemons/$LABEL.plist" ]; then
  say "There is a LaunchDaemon: sudo launchctl bootout system/$LABEL && sudo rm /Library/LaunchDaemons/$LABEL.plist"
fi
if [ "${KEEP:-1}" = 0 ]; then rm -rf "$APP_DIR"; say "Deleted $APP_DIR (model and key included)."; fi
say "Service removed."
