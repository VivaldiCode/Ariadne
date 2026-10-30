#!/bin/bash
# Verifica o serviço: /health, a chave (401 sem ela ou errada, 200 com ela) e um pedido curto de verdade.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source ./lib.sh
load_lock
PORT="$(bind_port)"
URL="http://localhost:$PORT"

curl -fs -m 5 "$URL/health" >/dev/null && say "ok   /health" || die "/health does not respond at $URL"
# /health e /v1/models são públicos no llama-server; o que exige chave é o /v1/chat/completions.
PING='{"model":"'"$MODEL_ALIAS"'","max_tokens":1,"messages":[{"role":"user","content":"oi"}]}'
chat() { curl -s -m 120 -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' "$@" -d "$PING" "$URL/v1/chat/completions"; }
code="$(chat)"
[ "$code" = "401" ] && say "ok   no key: 401" || die "without a key it should return 401 but returned $code (the key is not being enforced!)"
KEY="$(cat "$KEY_FILE")"
code="$(chat -H "Authorization: Bearer wrong-$KEY")"
[ "$code" = "401" ] && say "ok   wrong key: 401" || die "with a wrong key it should return 401 but returned $code"
code="$(chat -H "Authorization: Bearer $KEY")"
[ "$code" = "200" ] && say "ok   with key: 200" || die "with the key it should return 200 but returned $code"
reply="$(curl -s -m 120 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' "$URL/v1/chat/completions" \
  -d '{"model":"'"$MODEL_ALIAS"'","temperature":0,"max_tokens":16,"messages":[{"role":"user","content":"Responde só com a palavra: pronto"}]}')"
echo "$reply" | python3 -c 'import sys,json; r=json.load(sys.stdin); print("ok   request:", r["choices"][0]["message"]["content"].strip()[:40], "|", r["usage"]["total_tokens"], "tokens")'
