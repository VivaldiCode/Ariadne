<?php
namespace local_ariadne\external;

use core_external\external_api;
use core_external\external_function_parameters;
use core_external\external_value;

defined('MOODLE_INTERNAL') || die();

class url_blocked extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'urls' => new external_value(PARAM_RAW, 'JSON: lista de URL'),
        ]);
    }

    public static function execute(string $urls): string {
        $params = self::validate_parameters(self::execute_parameters(), ['urls' => $urls]);
        $context = \context_system::instance();
        self::validate_context($context);
        require_capability('moodle/course:create', $context);
        $list = json_decode($params['urls'], true);
        if (!is_array($list)) {
            throw new \moodle_exception('generalexceptionmessage', 'error', '', 'urls não é uma lista JSON');
        }
        return json_encode(\local_ariadne\publisher::url_blocked($list));
    }

    public static function execute_returns(): external_value {
        return new external_value(PARAM_RAW, 'JSON: URL -> bloqueado');
    }
}
