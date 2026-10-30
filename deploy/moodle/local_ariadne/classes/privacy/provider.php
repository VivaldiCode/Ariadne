<?php
namespace local_ariadne\privacy;

defined('MOODLE_INTERNAL') || die();

/** O plugin não guarda dados pessoais. */
class provider implements \core_privacy\local\metadata\null_provider {
    public static function get_reason(): string {
        return 'privacy:metadata';
    }
}
