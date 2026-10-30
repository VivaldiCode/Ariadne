<?php
namespace local_ariadne\external;

use core_external\external_api;
use core_external\external_function_parameters;
use core_external\external_value;

defined('MOODLE_INTERNAL') || die();

class delete_course extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'idnumber' => new external_value(PARAM_RAW, 'idnumber do curso («ariadne:…»)'),
        ]);
    }

    public static function execute(string $idnumber): bool {
        $params = self::validate_parameters(self::execute_parameters(), ['idnumber' => $idnumber]);
        self::validate_context(\context_system::instance());
        return \local_ariadne\publisher::delete($params['idnumber']);
    }

    public static function execute_returns(): external_value {
        return new external_value(PARAM_BOOL, 'se o curso existia e foi apagado');
    }
}
