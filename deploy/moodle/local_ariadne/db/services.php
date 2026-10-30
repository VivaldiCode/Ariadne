<?php
// Funções de web service do Ariadne. O serviço "ariadne" (criado por cli/bootstrap.php) inclui-as.

defined('MOODLE_INTERNAL') || die();

$functions = [
    'local_ariadne_ping' => [
        'classname' => 'local_ariadne\external\ping',
        'methodname' => 'execute',
        'description' => 'Versões do Moodle, do CodeRunner e do Jobe configurado.',
        'type' => 'read',
        'ajax' => false,
    ],
    'local_ariadne_publish_course' => [
        'classname' => 'local_ariadne\external\publish_course',
        'methodname' => 'execute',
        'description' => 'Cria ou atualiza um curso (secções, páginas e quizzes CodeRunner) a partir de um JSON do Ariadne.',
        'type' => 'write',
        'ajax' => false,
    ],
    'local_ariadne_delete_course' => [
        'classname' => 'local_ariadne\external\delete_course',
        'methodname' => 'execute',
        'description' => 'Apaga um curso publicado pelo Ariadne (idnumber «ariadne:…»).',
        'type' => 'write',
        'ajax' => false,
    ],
    'local_ariadne_url_blocked' => [
        'classname' => 'local_ariadne\external\url_blocked',
        'methodname' => 'execute',
        'description' => 'Diz que destinos o Moodle bloqueia nos pedidos que faz (proteção contra SSRF).',
        'type' => 'read',
        'ajax' => false,
    ],
];
