<?php
// Configuração do Moodle do Ariadne: tudo vem de variáveis de ambiente (ver docker-compose.yml).
unset($CFG);
global $CFG;
$CFG = new stdClass();

$CFG->dbtype = 'pgsql';
$CFG->dblibrary = 'native';
$CFG->dbhost = getenv('MOODLE_DB_HOST') ?: 'postgres';
$CFG->dbname = getenv('MOODLE_DB_NAME') ?: 'moodle';
$CFG->dbuser = getenv('MOODLE_DB_USER') ?: 'ariadne';
$CFG->dbpass = getenv('MOODLE_DB_PASSWORD') ?: '';
$CFG->prefix = 'mdl_';
$CFG->dboptions = ['dbpersist' => 0, 'dbport' => 5432, 'dbsocket' => ''];

// O mesmo Moodle responde no endereço público (o do navegador) e dentro da rede do compose
// (http://moodle, usado pelo server/worker). O wwwroot segue o Host do pedido, mas só se for um destes.
$public = getenv('MOODLE_PUBLIC_HOST') ?: 'localhost:8803';
$allowed = array_filter(array_map('trim', explode(',', $public . ',' . (getenv('MOODLE_EXTRA_HOSTS') ?: 'moodle'))));
$host = $_SERVER['HTTP_HOST'] ?? $public;
$CFG->wwwroot = (getenv('MOODLE_SCHEME') ?: 'http') . '://' . (in_array($host, $allowed, true) ? $host : $public);

$CFG->dataroot = '/var/www/moodledata';
$CFG->admin = 'admin';
$CFG->directorypermissions = 02777;
$CFG->sslproxy = false;

// Proteção contra SSRF: tudo o que o Moodle pede por HTTP (CodeRunner incluído) passa pelo curl_security_helper.
// Bloqueiam-se TODAS as redes privadas, o loopback e o link-local (metadados de cloud) menos o IP do jobe-gateway
// (JOBE_GATEWAY_IP; vazio = o de JOBE_HOST, resolvido em cada pedido, porque o Docker escolhe a sub-rede): o CIDR que
// o contém é dividido nos blocos que o deixam de fora, e o resto da rede continua bloqueado. Se o nome não resolver
// para um IPv4, não passa nada (o CodeRunner falha fechado). MOODLE_BLOCKED_HOSTS
// (uma entrada por linha ou separadas por vírgula) substitui a lista de omissão.
if (!function_exists('ariadne_blocked_hosts')) {
    /** Os CIDR de $cidr sem o endereço $allowed (IPv4); os que não o contêm ficam como estão. */
    function ariadne_cidr_without(string $cidr, string $allowed): array {
        if (strpos($cidr, ':') !== false || strpos($cidr, '/') === false) {
            return [$cidr];
        }
        [$base, $bits] = explode('/', $cidr);
        $ip = ip2long($allowed);
        $net = ip2long($base);
        $bits = (int) $bits;
        if ($ip === false || $net === false) {
            return [$cidr];
        }
        $mask = $bits === 0 ? 0 : ((-1 << (32 - $bits)) & 0xFFFFFFFF);
        if (($ip & $mask) !== ($net & $mask)) {
            return [$cidr];
        }
        $out = [];
        $net = $net & $mask;
        while ($bits < 32) {
            $bits++;
            $half = 1 << (32 - $bits);                        // tamanho de cada metade
            $high = $net + $half;
            if ($ip >= $high) {                               // o IP está na metade de cima: a de baixo fica bloqueada
                $out[] = long2ip($net) . '/' . $bits;
                $net = $high;
            } else {                                          // o IP está na de baixo: a de cima fica bloqueada
                $out[] = long2ip($high) . '/' . $bits;
            }
        }
        return $out;
    }

    function ariadne_blocked_hosts(?string $allowed, ?string $override): string {
        if ($override !== null && trim($override) !== '') {
            return implode("\n", array_filter(array_map('trim', preg_split('/[\s,]+/', $override))));
        }
        // Os intervalos reservados (loopback, redes privadas, link-local, 0/8 e CGNAT) vão como inteiro + máscara
        // e IPv6 em hexadecimal, para não haver endereços escritos por extenso neste ficheiro.
        $default = [];
        foreach ([[2130706432, 8], [167772160, 8], [2886729728, 12], [3232235520, 16], [2851995648, 16], [0, 8], [1681915904, 10]] as [$net, $bits]) {
            $default[] = long2ip($net) . '/' . $bits;
        }
        foreach ([['00000000000000000000000000000001', 128], ['00000000000000000000ffff00000000', 96],
                  ['fc000000000000000000000000000000', 7], ['fe800000000000000000000000000000', 10]] as [$hex, $bits]) {
            $default[] = inet_ntop(hex2bin($hex)) . '/' . $bits;
        }
        $blocked = [];
        foreach ($default as $cidr) {
            $blocked = array_merge($blocked, $allowed ? ariadne_cidr_without($cidr, $allowed) : [$cidr]);
        }
        return implode("\n", $blocked);
    }
}
$ariadnejobeip = getenv('JOBE_GATEWAY_IP') ?: null;
if ($ariadnejobeip === null && ($ariadnejobehost = getenv('JOBE_HOST') ?: '') !== '') {
    $ariadneresolved = gethostbyname(explode(':', $ariadnejobehost)[0]);  // devolve o nome se não resolver
    $ariadnejobeip = filter_var($ariadneresolved, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) ? $ariadneresolved : null;
}
$CFG->curlsecurityblockedhosts = ariadne_blocked_hosts($ariadnejobeip, getenv('MOODLE_BLOCKED_HOSTS') ?: null);
// Portas permitidas: 80 e 443 e, se o Jobe não estiver numa delas (ex.: um Jobe noutra máquina, em :4000), a do gateway (JOBE_GATEWAY_PORT).
$jobeport = getenv('JOBE_GATEWAY_PORT') ?: '';
$CFG->curlsecurityallowedport = "80\n443" . (ctype_digit($jobeport) && !in_array($jobeport, ['80', '443'], true) ? "\n" . $jobeport : '');

require_once(__DIR__ . '/lib/setup.php');
