#!/usr/bin/env bash
# Arranque do Jobe do Ariadne.
#
# O código dos alunos corre como os utilizadores `jobeNN` (grupo `jobe`, gid 500) e o Jobe não lhes corta a rede:
# podiam chamar o jobe-gateway (ou a API do próprio Jobe, em localhost) e, de lá, o que estivesse à vista.
# Aqui recusam-se TODAS as ligações novas desses utilizadores (loopback incluído); o Apache (www-data) e o resto
# não são afetados, e as respostas aos pedidos do gateway seguem pelas ligações já estabelecidas.
# Falha fechado: se as regras não entram, o contentor não arranca. Precisa de NET_ADMIN (só aqui, no arranque:
# o código dos alunos corre sem capacidades).
set -euo pipefail

JOBE_GID="${JOBE_GID:-$(getent group jobe | cut -d: -f3)}"

rules() {
    local ipt="$1"
    "$ipt" -w -F OUTPUT
    "$ipt" -w -A OUTPUT -m owner --gid-owner "$JOBE_GID" -j REJECT
}
rules iptables
rules ip6tables || echo "[ariadne] ip6tables unavailable (no IPv6 in the container): skipped" >&2

# Confirma que a regra ficou mesmo (e não só que o comando saiu bem).
iptables -w -S OUTPUT | grep -q -- "--gid-owner $JOBE_GID" \
    || { echo "[ariadne] jobeNN network rule missing" >&2; exit 1; }

mkdir -p /run/lock/apache2 /run/apache2 /var/www/html/jobe/writable/{cache,logs,session,uploads,debugbar}
chown -R www-data /run/lock/apache2 /var/www/html/jobe/writable
exec /usr/sbin/apache2ctl -D FOREGROUND
