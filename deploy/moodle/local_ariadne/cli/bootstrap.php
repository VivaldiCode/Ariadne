<?php
// Prepara o Moodle para o Ariadne (idempotente): web services REST, serviço "ariadne", utilizador do
// serviço e o CodeRunner a apontar ao Jobe. Corre no arranque do contentor, depois da instalação.
//
// Variáveis de ambiente: MOODLE_WS_USER, MOODLE_WS_PASSWORD, JOBE_HOST, JOBE_APIKEY.

define('CLI_SCRIPT', true);
require(__DIR__ . '/../../../config.php');
require_once($CFG->libdir . '/clilib.php');
require_once($CFG->dirroot . '/user/lib.php');
require_once($CFG->dirroot . '/webservice/lib.php');

$username = getenv('MOODLE_WS_USER') ?: 'ariadnews';
$password = getenv('MOODLE_WS_PASSWORD');
if (!$password) {
    cli_error('MOODLE_WS_PASSWORD em falta');
}

// 1. Web services REST.
set_config('enablewebservices', 1);
\core\plugininfo\webservice::enable_plugin('rest', 1);

// 2. CodeRunner a falar com o Jobe do compose (nunca o servidor público por omissão).
set_config('jobe_host', getenv('JOBE_HOST') ?: 'jobe-gateway', 'qtype_coderunner');
set_config('jobe_apikey', getenv('JOBE_APIKEY') ?: '', 'qtype_coderunner');
set_config('jobesandbox_enabled', 1, 'qtype_coderunner');

// 3. Utilizador do serviço.
$user = $DB->get_record('user', ['username' => $username, 'mnethostid' => $CFG->mnet_localhost_id]);
if (!$user) {
    $user = (object) [
        'username' => $username, 'password' => $password, 'firstname' => 'Ariadne', 'lastname' => 'Serviço',
        'email' => 'ariadne@localhost.invalid', 'auth' => 'manual', 'confirmed' => 1,
        'mnethostid' => $CFG->mnet_localhost_id, 'lang' => 'en',
    ];
    $user->id = user_create_user($user, true, false);
    echo "Utilizador $username criado\n";
} else {
    update_internal_user_password($user, $password);
}
// 4. O papel «Ariadne»: sem arquétipo, só com as capacidades que o `local_ariadne` e a verificação
// dos exercícios usam. Não é gestor nem administrador: não gere utilizadores, inscrições, definições
// nem cursos que não sejam seus (o plugin só toca em cursos «ariadne:…»).
$context = context_system::instance();
const ARIADNE_CAPABILITIES = [
    // Web services e token.
    'moodle/webservice:createtoken', 'webservice/rest:use',
    // Cursos: criar, atualizar e apagar os seus, ver o conteúdo, gerir atividades e secções.
    'moodle/course:create', 'moodle/course:update', 'moodle/course:delete', 'moodle/course:view',
    'moodle/course:viewhiddencourses', 'moodle/course:manageactivities', 'moodle/course:movesections',
    'moodle/course:sectionvisibility', 'moodle/course:viewhiddenactivities',
    // Atividades que se criam (páginas e quizzes) e a estrutura dos quizzes.
    'mod/page:addinstance', 'mod/quiz:addinstance', 'mod/quiz:manage',
    // Perguntas: importar para o banco do curso e usar nos quizzes.
    'moodle/question:add', 'moodle/question:editall', 'moodle/question:viewall',
    'moodle/question:useall', 'moodle/question:managecategory', 'moodle/question:movemine',
    'moodle/question:editmine', 'moodle/question:tagall',
    // Tentativas (para confirmar a correção automática dos exercícios).
    'mod/quiz:view', 'mod/quiz:attempt', 'mod/quiz:preview', 'mod/quiz:reviewmyattempts',
    'mod/page:view',
];
$role = $DB->get_record('role', ['shortname' => 'ariadne']);
if (!$role) {
    $roleid = create_role('Ariadne', 'ariadne', 'Serviço do Ariadne (cursos e correção)', '');
    echo "Papel ariadne criado\n";
} else {
    $roleid = (int) $role->id;
    $DB->set_field('role', 'archetype', '', ['id' => $roleid]);  // um papel de versões anteriores
}
set_role_contextlevels($roleid, [CONTEXT_SYSTEM]);
// O que o papel tem é exatamente esta lista: tira-se o resto (incluído o que um arquétipo trouxe).
[$insql, $inparams] = $DB->get_in_or_equal(ARIADNE_CAPABILITIES, SQL_PARAMS_NAMED, 'cap', false);
$DB->delete_records_select('role_capabilities', "roleid = :roleid AND capability $insql", ['roleid' => $roleid] + $inparams);
foreach (ARIADNE_CAPABILITIES as $capability) {
    if (!$DB->record_exists('capabilities', ['name' => $capability])) {
        echo "Aviso: a capacidade $capability não existe nesta versão\n";
        continue;
    }
    assign_capability($capability, CAP_ALLOW, $roleid, $context->id, true);
}
role_assign($roleid, $user->id, $context->id);
// O utilizador do serviço não tem mais nenhum papel nem é administrador.
foreach ($DB->get_records('role_assignments', ['userid' => $user->id]) as $assignment) {
    if ((int) $assignment->roleid !== $roleid) {
        role_unassign($assignment->roleid, $user->id, $assignment->contextid);
    }
}
$admins = array_filter(explode(',', (string) $CFG->siteadmins), fn($id) => (int) $id !== (int) $user->id);
if (count($admins) !== count(array_filter(explode(',', (string) $CFG->siteadmins)))) {
    set_config('siteadmins', implode(',', $admins));
}
accesslib_clear_all_caches(true);

// 5. Serviço externo «ariadne» com as funções que o Ariadne usa, e só essas.
const ARIADNE_FUNCTIONS = [
    'local_ariadne_ping', 'local_ariadne_publish_course', 'local_ariadne_delete_course',
    'local_ariadne_url_blocked', 'core_course_get_contents',
    'mod_quiz_start_attempt', 'mod_quiz_get_attempt_data', 'mod_quiz_process_attempt',
    'mod_quiz_get_attempt_review',
];
$manager = new webservice();
$service = $DB->get_record('external_services', ['shortname' => 'ariadne']);
if (!$service) {
    $service = (object) [
        'name' => 'Ariadne', 'shortname' => 'ariadne', 'enabled' => 1, 'restrictedusers' => 0,
        'component' => null, 'timecreated' => time(), 'timemodified' => time(),
        'downloadfiles' => 0, 'uploadfiles' => 0, 'requiredcapability' => null,
    ];
    $service->id = $manager->add_external_service($service);
    echo "Serviço ariadne criado\n";
} elseif (!$service->enabled || $service->restrictedusers) {
    $service->enabled = 1;
    $service->restrictedusers = 0;
    $manager->update_external_service($service);
}
// Funções que já lá estavam e o Ariadne já não usa (versões anteriores) saem.
[$insql, $inparams] = $DB->get_in_or_equal(ARIADNE_FUNCTIONS, SQL_PARAMS_NAMED, 'fn', false);
$DB->delete_records_select('external_services_functions', "externalserviceid = :id AND functionname $insql", ['id' => $service->id] + $inparams);
foreach (ARIADNE_FUNCTIONS as $function) {
    if (!$DB->record_exists('external_functions', ['name' => $function])) {
        echo "Aviso: a função $function não existe nesta versão\n";
        continue;
    }
    if (!$manager->service_function_exists($function, $service->id)) {
        $manager->add_external_function_to_service($function, $service->id);
    }
}

// 6. O token do serviço, criado aqui para ter validade e restrição de IP (o que o login/token.php
// cria não tem IP e dura o `tokenduration` do Moodle). `ariadne moodle bootstrap` pede-o a
// login/token.php, que devolve este. MOODLE_TOKEN_IPS: redes do server e do worker (vírgulas;
// vazio = qualquer IP); MOODLE_TOKEN_DAYS: validade (omissão 365; renova-se a 30 dias do fim).
$ips = trim((string) getenv('MOODLE_TOKEN_IPS'));
$days = max(1, (int) (getenv('MOODLE_TOKEN_DAYS') ?: 365));
$tokens = $DB->get_records('external_tokens', ['userid' => $user->id, 'externalserviceid' => $service->id, 'tokentype' => EXTERNAL_TOKEN_PERMANENT]);
$good = null;
foreach ($tokens as $token) {
    // Só serve um token nosso (o que o login/token.php cria sozinho não tem IP nem a nossa validade).
    $stillvalid = $token->name === 'ariadne' && $token->validuntil && $token->validuntil > time() + 30 * DAYSECS;
    if ($good === null && $stillvalid && (string) $token->iprestriction === $ips) {
        $good = $token;
    } else {
        $DB->delete_records('external_tokens', ['id' => $token->id]);  // expirado, ou com outro IP
    }
}
if ($good === null) {
    \core_external\util::generate_token(
        EXTERNAL_TOKEN_PERMANENT, $service, $user->id, $context, time() + $days * DAYSECS, $ips, 'ariadne'
    );
    echo "Token do serviço criado (validade $days dias" . ($ips !== '' ? ", IP $ips" : '') . ")\n";
}
purge_caches(['muc' => true]);
echo "Ariadne: bootstrap do Moodle concluído\n";
