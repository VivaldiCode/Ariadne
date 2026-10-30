#!/usr/bin/env bash
# Arranque do Moodle do Ariadne (corre antes do Apache, a partir do entrypoint da imagem base).
# Idempotente: cria a base se faltar, instala o Moodle na 1.ª vez, aplica upgrades/plugins novos e
# prepara os web services. Os dados de acesso vêm de variáveis de ambiente (docker-compose.yml).
set -euo pipefail

cd /var/www/html
: "${MOODLE_DB_HOST:=postgres}" "${MOODLE_DB_NAME:=moodle}" "${MOODLE_DB_USER:=ariadne}"
: "${MOODLE_DB_PASSWORD:?MOODLE_DB_PASSWORD em falta}"
: "${MOODLE_ADMIN_USER:=admin}" "${MOODLE_ADMIN_PASSWORD:?MOODLE_ADMIN_PASSWORD em falta}"
: "${MOODLE_ADMIN_EMAIL:=admin@localhost.invalid}" "${MOODLE_SITE_NAME:=Ariadne}"
export MOODLE_DB_HOST MOODLE_DB_NAME MOODLE_DB_USER MOODLE_DB_PASSWORD
export MOODLE_ADMIN_USER MOODLE_ADMIN_PASSWORD MOODLE_ADMIN_EMAIL MOODLE_SITE_NAME

as_www() { runuser -u www-data -- "$@"; }

mkdir -p /var/www/moodledata
chown -R www-data:www-data /var/www/moodledata

echo "[ariadne] ensuring database '$MOODLE_DB_NAME'"
php -r '
$db = getenv("MOODLE_DB_NAME");
for ($i = 0; $i < 60; $i++) {
    $c = @pg_connect("host=".getenv("MOODLE_DB_HOST")." port=5432 user=".getenv("MOODLE_DB_USER")." password=".getenv("MOODLE_DB_PASSWORD")." dbname=postgres connect_timeout=3");
    if ($c) break;
    sleep(2);
}
if (!$c) { fwrite(STDERR, "Postgres unreachable\n"); exit(1); }
$r = pg_query_params($c, "SELECT 1 FROM pg_database WHERE datname = $1", [$db]);
if (!pg_fetch_row($r)) {
    pg_query($c, "CREATE DATABASE ".pg_escape_identifier($c, $db)." ENCODING \x27UTF8\x27 TEMPLATE template0");
    echo "database created\n";
}
'

# Instalado = a tabela de configuração do Moodle existe na base.
installed() {
    php -r '
    $c = pg_connect("host=".getenv("MOODLE_DB_HOST")." port=5432 user=".getenv("MOODLE_DB_USER")." password=".getenv("MOODLE_DB_PASSWORD")." dbname=".getenv("MOODLE_DB_NAME"));
    $r = pg_query($c, "SELECT to_regclass(\x27mdl_config\x27)");
    $row = pg_fetch_row($r);
    exit($row[0] ? 0 : 1);
    '
}

if installed; then
    echo "[ariadne] Moodle already installed: upgrade (if any)"
    as_www php admin/cli/upgrade.php --non-interactive
else
    echo "[ariadne] installing Moodle (the first run takes a few minutes)"
    # As opções vão pelo ambiente (install-database.php): a palavra-passe não fica nos argumentos do processo.
    as_www php /usr/local/share/ariadne/install-database.php
fi

echo "[ariadne] bootstrapping web services and CodeRunner"
as_www php local/ariadne/cli/bootstrap.php

# O cron do Moodle (tarefas agendadas e adiadas) de minuto a minuto, em segundo plano.
nohup setsid bash -c 'while true; do runuser -u www-data -- php /var/www/html/admin/cli/cron.php >/dev/null 2>&1; sleep 60; done' >/dev/null 2>&1 &
echo "[ariadne] Moodle ready"
