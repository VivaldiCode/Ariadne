#!/bin/sh
# Corre no arranque do nginx (/docker-entrypoint.d/, antes do envsubst dos templates): escreve o `resolver`
# a partir do nameserver que o próprio contentor tem em /etc/resolv.conf (o DNS embutido do Docker, que
# resolve «jobe» e os outros serviços do compose). Assim o endereço não fica escrito no código nem em
# variáveis, e funciona em qualquer rede/compose. Sem nameserver o arranque falha de propósito.
set -eu
ns=$(awk '$1 == "nameserver" { print $2; exit }' /etc/resolv.conf)
[ -n "$ns" ] || { echo "15-resolver: no nameserver in /etc/resolv.conf" >&2; exit 1; }
case "$ns" in *:*) ns="[$ns]" ;; esac
# no contexto http: vale para o server/location do template (a resolução dinâmica, porque o Jobe pode ser recriado)
printf 'resolver %s valid=10s ipv6=off;\n' "$ns" > /etc/nginx/conf.d/00-resolver.conf
echo "15-resolver: resolver written from /etc/resolv.conf"
