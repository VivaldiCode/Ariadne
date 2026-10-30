<?php
namespace local_ariadne\external;

use core_external\external_api;
use core_external\external_function_parameters;
use core_external\external_value;

defined('MOODLE_INTERNAL') || die();

class publish_course extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'payload' => new external_value(PARAM_RAW, 'JSON do curso (ver local_ariadne\publisher)'),
        ]);
    }

    public static function execute(string $payload): string {
        $params = self::validate_parameters(self::execute_parameters(), ['payload' => $payload]);
        $context = \context_system::instance();
        self::validate_context($context);
        require_capability('moodle/course:create', $context);
        $data = json_decode($params['payload'], true);
        if (!is_array($data)) {
            throw new \moodle_exception('generalexceptionmessage', 'error', '', 'o payload não é JSON válido');
        }
        return json_encode(\local_ariadne\publisher::publish($data));
    }

    public static function execute_returns(): external_value {
        return new external_value(PARAM_RAW, 'JSON com o resultado');
    }
}
