<?php
namespace local_ariadne\external;

use core_external\external_api;
use core_external\external_function_parameters;
use core_external\external_value;

defined('MOODLE_INTERNAL') || die();

class ping extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([]);
    }

    public static function execute(): string {
        global $CFG;
        $context = \context_system::instance();
        self::validate_context($context);
        require_capability('moodle/course:create', $context);
        $coderunner = \core_plugin_manager::instance()->get_plugin_info('qtype_coderunner');
        return json_encode([
            'moodle' => $CFG->release,
            'plugin' => get_config('local_ariadne', 'version'),
            'coderunner' => $coderunner ? $coderunner->release : null,
            'jobe_host' => get_config('qtype_coderunner', 'jobe_host'),
        ]);
    }

    public static function execute_returns(): external_value {
        return new external_value(PARAM_RAW, 'JSON');
    }
}
