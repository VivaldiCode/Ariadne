#!/usr/bin/env bash
# Junta os tokens do DS e o CSS do tema num só ficheiro (theme.css), pronto para colar em
# Administração → Tema → «CSS Override» do Wiki.js.
set -euo pipefail
cd "$(dirname "$0")"
cat ../tema-ariadne/tokens.css wiki.css > theme.css
echo "theme.css: $(wc -c < theme.css) bytes"
