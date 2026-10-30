<?php
// Corre admin/cli/install_database.php com as opções vindas do ambiente: assim a palavra-passe do administrador
// não aparece nos argumentos do processo (`ps`, /proc/<pid>/cmdline), onde qualquer utilizador do contentor a lia.
// O cli_get_params() do Moodle lê $_SERVER['argv'], que aqui se monta em memória.
$need = static function (string $name): string {
    $value = getenv($name);
    if ($value === false || $value === '') {
        fwrite(STDERR, "$name em falta\n");
        exit(1);
    }
    return $value;
};
$_SERVER['argv'] = [
    'install_database.php',
    '--lang=en',
    '--adminuser=' . $need('MOODLE_ADMIN_USER'),
    '--adminpass=' . $need('MOODLE_ADMIN_PASSWORD'),
    '--adminemail=' . ($need('MOODLE_ADMIN_EMAIL')),
    '--fullname=' . $need('MOODLE_SITE_NAME'),
    '--shortname=ariadne',
    '--agree-license',
];
$_SERVER['argc'] = count($_SERVER['argv']);
require '/var/www/html/admin/cli/install_database.php';
