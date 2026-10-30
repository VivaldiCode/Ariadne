#!/bin/bash
# Copia a chave de API do curador para a área de transferência, sem a imprimir. Cola-a no dashboard
# (Integrações → Curador → Chave de API) e limpa a área de transferência a seguir.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source ./lib.sh
[ -s "$KEY_FILE" ] || die "no key yet: run ./install.sh"
pbcopy < "$KEY_FILE"
say "Key copied (you won't see it here). Paste it in the dashboard; then: pbcopy < /dev/null"
