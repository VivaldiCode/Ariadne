#!/bin/sh
# Cria o .env do standalone a partir do .env.example, com cada segredo (vazio ou `change-me`) gerado por openssl rand.
# Uso (na raiz do repositório): deploy/standalone/init-env.sh [ficheiro]   (por omissão: .env)
# Nunca sobrescreve um valor que já lá esteja: corre-se outra vez sem perigo (só preenche o que falta).
# Os valores não se imprimem; ficam só no ficheiro (modo 600).
set -eu
ENV_FILE=${1:-.env}
[ -f "$ENV_FILE" ] || cp .env.example "$ENV_FILE"
chmod 600 "$ENV_FILE"

hex() { openssl rand -hex "$1"; }
# Moodle e Kavita exigem maiúscula, minúscula, número e símbolo.
strong() { printf '%sAr1!' "$(openssl rand -hex 12)"; }

fill() {  # fill NOME VALOR: só se a linha NOME= estiver vazia, com o placeholder `change-me`, ou não existir
  name=$1 value=$2
  if grep -q "^$name=." "$ENV_FILE" && ! grep -qx "$name=change-me" "$ENV_FILE"; then return; fi
  if grep -q "^$name=" "$ENV_FILE"; then
    tmp=$(mktemp)
    awk -v n="$name" -v v="$value" 'BEGIN { FS = OFS = "=" } $1 == n && !done { print n, v; done = 1; next } { print }' \
      "$ENV_FILE" > "$tmp" && cat "$tmp" > "$ENV_FILE" && rm -f "$tmp"
  else
    printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
  fi
  echo "  $name generated"
}

fill POSTGRES_PASSWORD "$(hex 24)"
fill ARIADNE_MASTER_KEY "$(openssl rand -base64 32)"
fill WIKIJS_ADMIN_PASSWORD "$(hex 16)"
fill MOODLE_ADMIN_PASSWORD "$(strong)"
fill MOODLE_WS_PASSWORD "$(strong)"
fill JOBE_API_KEY "$(hex 24)"
fill CALIBRE_API_KEY "$(hex 24)"
fill KAVITA_ADMIN_PASSWORD "$(strong)"
fill CURATOR_API_KEY "$(hex 24)"
fill GPU_GATEWAY_DB_PASSWORD "$(hex 24)"

# Ports only open on loopback: Docker does not accept the name `localhost`, so it is resolved here, once, to IPv4.
loopback() {
  python3 -c 'import socket; print(socket.gethostbyname("localhost"))' 2>/dev/null \
    || getent ahostsv4 localhost 2>/dev/null | awk 'NR == 1 { print $1 }'
}
if ! grep -q "^PUBLISH_ADDRESS=." "$ENV_FILE"; then
  addr=$(loopback)
  [ -n "$addr" ] || { echo "Could not resolve localhost: set PUBLISH_ADDRESS in $ENV_FILE by hand." >&2; exit 1; }
  fill PUBLISH_ADDRESS "$addr"
fi
echo "Done: $ENV_FILE. Also store ARIADNE_MASTER_KEY outside this file (without it the vault cannot be read)."
