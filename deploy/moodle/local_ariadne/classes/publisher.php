<?php
// Publica um curso do Ariadne no Moodle, de forma idempotente.
//
// Entrada (array decodificado do JSON):
//   idnumber (obrigatoriamente «ariadne:…»), shortname, fullname, summary (HTML)
//   sections[]: key, name, summary (HTML),
//     activities[]: key, kind ('page'|'quiz'), name, hash,   (`null` = esta chamada não toca nelas)
//       page: html
//       quiz: intro (HTML), questions_xml (Moodle XML: CodeRunner e multichoice)
//   prune[]: as chaves que ficam; se vier, as outras atividades com chave saem do curso
//
// Uma publicação grande faz-se em várias chamadas (uma por secção ou parte dela, e a última só com
// `prune`): cada uma é curta e idempotente. Só se mexe em cursos com `idnumber` «ariadne:…», com as
// capacidades verificadas no contexto do curso, e uma chamada por curso de cada vez (bloqueio).
//
// Cada atividade leva `key` (guardada em course_modules.idnumber) e `hash` (conteúdo): se a atividade
// existe e o hash não mudou, não se toca em nada. Um quiz com tentativas não se reestrutura:
// fica como estava e o resultado diz `blocked_by_attempts`.

namespace local_ariadne;

defined('MOODLE_INTERNAL') || die();

class publisher {
    private const PREFIX = 'ariadne:';
    private const LOCK_TIMEOUT = 120;

    /** Valores por omissão de um quiz (os do gerador de testes do Moodle), com a resposta certa
     *  (a solução oficial, guardada como «resposta de exemplo») escondida dos alunos. */
    private const QUIZ_DEFAULTS = [
        'timeopen' => 0, 'timeclose' => 0, 'attempts' => 0, 'attemptonlast' => 0,
        'decimalpoints' => 2, 'questiondecimalpoints' => -1,
        'attemptduring' => 1, 'correctnessduring' => 1, 'maxmarksduring' => 1, 'marksduring' => 1,
        'specificfeedbackduring' => 1, 'generalfeedbackduring' => 1, 'rightanswerduring' => 0,
        'overallfeedbackduring' => 0,
        'attemptimmediately' => 1, 'correctnessimmediately' => 1, 'maxmarksimmediately' => 1,
        'marksimmediately' => 1, 'specificfeedbackimmediately' => 1, 'generalfeedbackimmediately' => 1,
        'rightanswerimmediately' => 0, 'overallfeedbackimmediately' => 1,
        'attemptopen' => 1, 'correctnessopen' => 1, 'maxmarksopen' => 1, 'marksopen' => 1,
        'specificfeedbackopen' => 1, 'generalfeedbackopen' => 1, 'rightansweropen' => 0,
        'overallfeedbackopen' => 1,
        'attemptclosed' => 1, 'correctnessclosed' => 1, 'maxmarksclosed' => 1, 'marksclosed' => 1,
        'specificfeedbackclosed' => 1, 'generalfeedbackclosed' => 1, 'rightanswerclosed' => 0,
        'overallfeedbackclosed' => 1,
        'questionsperpage' => 0, 'shuffleanswers' => 1, 'sumgrades' => 0, 'grade' => 100,
        'timelimit' => 0, 'overduehandling' => 'autosubmit', 'graceperiod' => 86400,
        'quizpassword' => '', 'subnet' => '', 'browsersecurity' => '', 'delay1' => 0, 'delay2' => 0,
        'showuserpicture' => 0, 'showblocks' => 0,
    ];

    public static function publish(array $payload): array {
        global $CFG, $DB;
        require_once($CFG->dirroot . '/course/lib.php');
        require_once($CFG->dirroot . '/course/modlib.php');
        require_once($CFG->dirroot . '/mod/quiz/locallib.php');
        require_once($CFG->libdir . '/questionlib.php');

        foreach (['idnumber', 'shortname', 'fullname'] as $field) {
            if (empty($payload[$field]) || !is_string($payload[$field])) {
                throw new \moodle_exception('generalexceptionmessage', 'error', '', "falta o campo $field");
            }
        }
        if (strpos($payload['idnumber'], self::PREFIX) !== 0) {
            throw new \moodle_exception('generalexceptionmessage', 'error', '', 'o idnumber tem de começar por ' . self::PREFIX);
        }
        $factory = \core\lock\lock_config::get_lock_factory('local_ariadne');
        $lock = $factory->get_lock('publish_' . md5($payload['idnumber']), self::LOCK_TIMEOUT);
        if (!$lock) {
            throw new \moodle_exception('generalexceptionmessage', 'error', '', 'outra publicação deste curso está em curso; tenta mais tarde');
        }
        try {
            return self::publish_locked($payload);
        } finally {
            $lock->release();
        }
    }

    private static function publish_locked(array $payload): array {
        global $DB;
        $report = [
            'created' => 0, 'updated' => 0, 'unchanged' => 0, 'blocked_by_attempts' => 0,
            'removed' => 0, 'activities' => [], 'warnings' => [], 'course_status' => 'unchanged',
        ];
        $course = self::ensure_course($payload, $report);
        $coursecontext = \context_course::instance($course->id);
        require_capability('moodle/course:update', $coursecontext);
        require_capability('moodle/course:manageactivities', $coursecontext);
        $sections = $payload['sections'] ?? [];
        if ($sections) {
            course_create_sections_if_missing($course, range(0, count($sections)));
        }

        foreach ($sections as $index => $section) {
            $number = $index + 1;
            $row = $DB->get_record('course_sections', ['course' => $course->id, 'section' => $number], '*', MUST_EXIST);
            course_update_section($course, $row, [
                'name' => (string) ($section['name'] ?? ''),
                'summary' => (string) ($section['summary'] ?? ''),
                'summaryformat' => FORMAT_HTML,
            ]);
            foreach ($section['activities'] ?? [] as $activity) {
                $key = (string) $activity['key'];
                $report['activities'][$key] = self::ensure_activity($course, $number, $activity, $report);
            }
        }
        if (isset($payload['prune']) && is_array($payload['prune'])) {
            // O que já não está no curso canónico (só as atividades com chave nossa).
            $keep = array_flip(array_map('strval', $payload['prune']));
            $cms = $DB->get_records_select('course_modules', "course = ? AND idnumber <> ''", [$course->id]);
            foreach ($cms as $cm) {
                if (!isset($keep[$cm->idnumber])) {
                    course_delete_module($cm->id);
                    unset_config('hash_' . $cm->id, 'local_ariadne');
                    $report['removed']++;
                }
            }
        }
        rebuild_course_cache($course->id, true);
        $report['course'] = [
            'id' => (int) $course->id,
            'url' => (new \moodle_url('/course/view.php', ['id' => $course->id]))->out(false),
        ];
        return $report;
    }

    /** Apaga um curso publicado por nós (só `ariadne:…`, com a capacidade no contexto do curso). */
    public static function delete(string $idnumber): bool {
        global $CFG, $DB;
        require_once($CFG->dirroot . '/course/lib.php');
        if (strpos($idnumber, self::PREFIX) !== 0) {
            throw new \moodle_exception('generalexceptionmessage', 'error', '', 'o idnumber tem de começar por ' . self::PREFIX);
        }
        $course = $DB->get_record('course', ['idnumber' => $idnumber]);
        if (!$course) {
            return false;
        }
        require_capability('moodle/course:delete', \context_course::instance($course->id));
        return delete_course($course, false);
    }

    /** Diagnóstico: que destinos o Moodle bloqueia nos pedidos que faz (a proteção contra SSRF). */
    public static function url_blocked(array $urls): array {
        $helper = new \core\files\curl_security_helper();
        $result = [];
        foreach ($urls as $url) {
            $result[(string) $url] = (bool) $helper->url_is_blocked((string) $url);
        }
        return $result;
    }

    private static function ensure_course(array $payload, array &$report): \stdClass {
        global $CFG, $DB;
        $course = $DB->get_record('course', ['idnumber' => $payload['idnumber']]);
        $data = [
            'fullname' => $payload['fullname'],
            'shortname' => $payload['shortname'],
            'summary' => (string) ($payload['summary'] ?? ''),
            'summaryformat' => FORMAT_HTML,
            'idnumber' => $payload['idnumber'],
        ];
        if ($course) {
            $changed = false;
            foreach ($data as $field => $value) {
                if ($course->$field != $value) {
                    $changed = true;
                }
            }
            if ($changed) {
                $data['id'] = $course->id;
                update_course((object) $data);
                $report['course_status'] = 'updated';
            } else {
                $report['course_status'] = 'unchanged';
            }
            return get_course($course->id);
        }
        if ($DB->record_exists('course', ['shortname' => $payload['shortname']])) {
            throw new \moodle_exception('generalexceptionmessage', 'error', '', 'o shortname já é de outro curso: ' . $payload['shortname']);
        }
        require_capability('moodle/course:create', \context_system::instance());
        $data['category'] = $CFG->defaultrequestcategory ?? 1;
        $data['format'] = 'topics';
        $data['visible'] = 1;
        $created = create_course((object) $data);
        $report['course_status'] = 'created';
        return get_course($created->id);
    }

    private static function ensure_activity(\stdClass $course, int $sectionnumber, array $activity, array &$report): array {
        global $DB;
        $key = (string) $activity['key'];
        $kind = (string) $activity['kind'];
        $hash = (string) ($activity['hash'] ?? '');
        if (!in_array($kind, ['page', 'quiz'], true)) {
            throw new \moodle_exception('generalexceptionmessage', 'error', '', "tipo de atividade desconhecido: $kind");
        }
        // Procura-se só pela chave: se a lição mudou de tipo (página <-> quiz) a atividade antiga sai
        // e cria-se a nova, sem ficarem duas.
        $found = $DB->get_records_sql(
            "SELECT cm.*, m.name AS modname FROM {course_modules} cm JOIN {modules} m ON m.id = cm.module
              WHERE cm.course = ? AND cm.idnumber = ? ORDER BY cm.id",
            [$course->id, $key]
        );
        $cm = null;
        foreach ($found as $candidate) {
            if ($cm === null && $candidate->modname === $kind) {
                $cm = $candidate;  // a que serve (a mais antiga do tipo certo)
            } else {
                course_delete_module($candidate->id);  // de outro tipo, ou repetida
                unset_config('hash_' . $candidate->id, 'local_ariadne');
                $report['removed']++;
            }
        }
        if ($cm) {
            $section = $DB->get_record('course_sections', ['id' => $cm->section], '*', MUST_EXIST);
            if ((int) $section->section !== $sectionnumber) {
                moveto_module($cm, $DB->get_record('course_sections', ['course' => $course->id, 'section' => $sectionnumber], '*', MUST_EXIST));
            }
            $stored = get_config('local_ariadne', 'hash_' . $cm->id);
            if ($stored !== false && $stored === $hash && $hash !== '') {
                $report['unchanged']++;
                return self::describe($cm, $kind, 'unchanged');
            }
            $status = $kind === 'page'
                ? self::update_page($course, $cm, $activity)
                : self::update_quiz($course, $cm, $activity);
            if ($status === 'updated') {
                set_config('hash_' . $cm->id, $hash, 'local_ariadne');
                $report['updated']++;
            } else {
                $report['blocked_by_attempts']++;
                $report['warnings'][] = "$key: o quiz já tem tentativas e as perguntas mudaram; fica como estava";
            }
            return self::describe($cm, $kind, $status);
        }
        $cm = $kind === 'page'
            ? self::create_page($course, $sectionnumber, $activity)
            : self::create_quiz($course, $sectionnumber, $activity);
        set_config('hash_' . $cm->id, $hash, 'local_ariadne');
        $report['created']++;
        return self::describe($cm, $kind, 'created');
    }

    private static function describe(\stdClass $cm, string $kind, string $status): array {
        global $DB;
        $info = [
            'cmid' => (int) $cm->id,
            'kind' => $kind,
            'status' => $status,
            'url' => (new \moodle_url("/mod/$kind/view.php", ['id' => $cm->id]))->out(false),
            'instance' => (int) $cm->instance,
        ];
        if ($kind === 'quiz') {
            $info['questions'] = array_map('intval', array_keys(self::quiz_question_ids((int) $cm->instance)));
        }
        return $info;
    }

    // --- páginas ----------------------------------------------------------------------------

    private static function create_page(\stdClass $course, int $sectionnumber, array $activity): \stdClass {
        global $CFG;
        require_once($CFG->libdir . '/resourcelib.php');
        $info = (object) [
            'modulename' => 'page', 'module' => self::module_id('page'), 'course' => $course->id,
            'section' => $sectionnumber, 'visible' => 1, 'visibleoncoursepage' => 1,
            'cmidnumber' => (string) $activity['key'], 'name' => (string) $activity['name'],
            'intro' => '', 'introformat' => FORMAT_HTML, 'showdescription' => 0,
            'content' => (string) ($activity['html'] ?? ''), 'contentformat' => FORMAT_HTML,
            'display' => RESOURCELIB_DISPLAY_AUTO, 'printintro' => 0, 'printlastmodified' => 0,
            'revision' => 1, 'groupmode' => 0, 'groupingid' => 0, 'completion' => 0,
            'completionview' => 0, 'completionexpected' => 0, 'completiongradeitemnumber' => null,
        ];
        $result = add_moduleinfo($info, $course);
        return get_coursemodule_from_id('page', $result->coursemodule, 0, false, MUST_EXIST);
    }

    private static function update_page(\stdClass $course, \stdClass $cm, array $activity): string {
        global $DB;
        // Atualização direta: o formulário do Moodle não é preciso para texto HTML nosso.
        $DB->update_record('page', (object) [
            'id' => $cm->instance,
            'name' => (string) $activity['name'],
            'content' => (string) ($activity['html'] ?? ''),
            'contentformat' => FORMAT_HTML,
            'timemodified' => time(),
        ]);
        $DB->set_field('page', 'revision', $DB->get_field('page', 'revision', ['id' => $cm->instance]) + 1, ['id' => $cm->instance]);
        $DB->set_field('course_modules', 'idnumber', (string) $activity['key'], ['id' => $cm->id]);
        \course_modinfo::purge_course_module_cache($course->id, $cm->id);
        return 'updated';
    }

    // --- quizzes com perguntas CodeRunner ---------------------------------------------------

    private static function create_quiz(\stdClass $course, int $sectionnumber, array $activity): \stdClass {
        $info = (object) array_merge(self::QUIZ_DEFAULTS, [
            'modulename' => 'quiz', 'module' => self::module_id('quiz'), 'course' => $course->id,
            'section' => $sectionnumber, 'visible' => 1, 'visibleoncoursepage' => 1,
            'cmidnumber' => (string) $activity['key'], 'name' => (string) $activity['name'],
            'intro' => (string) ($activity['intro'] ?? ''), 'introformat' => FORMAT_HTML,
            'showdescription' => 0,
            'preferredbehaviour' => 'adaptivenopenalty',  // o CodeRunner usa a sua variante (não arquetípica)
            'navmethod' => QUIZ_NAVMETHOD_FREE, 'grademethod' => QUIZ_GRADEHIGHEST,
            'groupmode' => 0, 'groupingid' => 0, 'completion' => 0, 'completionview' => 0,
            'completionexpected' => 0, 'completiongradeitemnumber' => null,
            'timecreated' => time(), 'timemodified' => time(),
        ]);
        $result = add_moduleinfo($info, $course);
        $cm = get_coursemodule_from_id('quiz', $result->coursemodule, 0, false, MUST_EXIST);
        self::fill_quiz($course, $cm, (string) ($activity['questions_xml'] ?? ''));
        return $cm;
    }

    private static function update_quiz(\stdClass $course, \stdClass $cm, array $activity): string {
        global $DB;
        $quizid = (int) $cm->instance;
        if ($DB->record_exists('quiz_attempts', ['quiz' => $quizid])) {
            return 'blocked_by_attempts';
        }
        // Primeiro cria-se (se falhar, o quiz fica como estava), depois troca-se, e só no fim se apagam
        // as perguntas antigas.
        $new = self::import_questions($course, (string) ($activity['questions_xml'] ?? ''));
        $old = array_keys(self::quiz_question_ids($quizid));
        $quiz = $DB->get_record('quiz', ['id' => $quizid], '*', MUST_EXIST);
        $quiz->cmid = $cm->id;
        $transaction = $DB->start_delegated_transaction();
        try {
            $structure = \mod_quiz\structure::create_for_quiz(\mod_quiz\quiz_settings::create($quizid));
            for ($slot = count($old); $slot >= 1; $slot--) {
                $structure->remove_slot($slot);
            }
            foreach ($new as $questionid) {
                quiz_add_quiz_question($questionid, $quiz);
            }
            \mod_quiz\quiz_settings::create($quizid)->get_grade_calculator()->recompute_quiz_sumgrades();
            $DB->update_record('quiz', (object) [
                'id' => $quizid, 'name' => (string) $activity['name'],
                'intro' => (string) ($activity['intro'] ?? ''), 'introformat' => FORMAT_HTML,
                'timemodified' => time(),
            ]);
            $transaction->allow_commit();
        } catch (\Throwable $e) {
            try {
                $transaction->rollback($e);
            } catch (\Throwable $rolledback) {
                // a exceção volta a levantar-se abaixo
            }
            foreach ($new as $questionid) {
                question_delete_question($questionid);  // não ficam perguntas órfãs no banco
            }
            throw $e;
        }
        foreach ($old as $questionid) {
            question_delete_question($questionid);
        }
        $DB->set_field('course_modules', 'idnumber', (string) $activity['key'], ['id' => $cm->id]);
        \course_modinfo::purge_course_module_cache($course->id, $cm->id);
        return 'updated';
    }

    /** Importa o Moodle XML para o banco de perguntas do curso e devolve os ids das perguntas. */
    private static function import_questions(\stdClass $course, string $xml): array {
        global $CFG;
        require_once($CFG->dirroot . '/question/format.php');
        require_once($CFG->dirroot . '/question/format/xml/format.php');
        if ($xml === '') {
            return [];
        }
        $coursecontext = \context_course::instance($course->id);
        $category = question_get_default_category($coursecontext->id);
        if (!$category) {
            question_make_default_categories([$coursecontext]);
            $category = question_get_default_category($coursecontext->id);
        }
        $file = make_request_directory() . '/ariadne.xml';
        file_put_contents($file, $xml);
        $contexts = new \core_question\local\bank\question_edit_contexts($coursecontext);
        $format = new \qformat_xml();
        $format->setCategory($category);
        $format->setContexts($contexts->having_one_edit_tab_cap('import'));
        $format->setCourse($course);
        $format->setFilename($file);
        $format->setRealfilename('ariadne.xml');
        $format->setMatchgrades('error');
        $format->setCatfromfile(false);
        $format->setContextfromfile(false);
        $format->setStoponerror(true);
        $format->set_display_progress(false);
        ob_start();
        $ok = $format->importpreprocess() && $format->importprocess() && $format->importpostprocess();
        $output = trim(strip_tags(ob_get_clean()));
        if (!$ok) {
            foreach ($format->questionids as $questionid) {
                question_delete_question($questionid);
            }
            throw new \moodle_exception('generalexceptionmessage', 'error', '', 'importação das perguntas falhou: ' . $output);
        }
        return array_map('intval', $format->questionids);
    }

    private static function fill_quiz(\stdClass $course, \stdClass $cm, string $xml): void {
        global $DB;
        $quiz = $DB->get_record('quiz', ['id' => $cm->instance], '*', MUST_EXIST);
        $quiz->cmid = $cm->id;
        foreach (self::import_questions($course, $xml) as $questionid) {
            quiz_add_quiz_question($questionid, $quiz);
        }
        \mod_quiz\quiz_settings::create($quiz->id)->get_grade_calculator()->recompute_quiz_sumgrades();
    }

    /** id da pergunta => slot, pela ordem dos slots. */
    private static function quiz_question_ids(int $quizid): array {
        global $DB;
        $rows = $DB->get_records_sql(
            "SELECT q.id AS questionid, slot.slot
               FROM {quiz_slots} slot
               JOIN {question_references} qr ON qr.itemid = slot.id AND qr.component = 'mod_quiz' AND qr.questionarea = 'slot'
               JOIN {question_bank_entries} qbe ON qbe.id = qr.questionbankentryid
               JOIN {question_versions} qv ON qv.questionbankentryid = qbe.id
               JOIN {question} q ON q.id = qv.questionid
              WHERE slot.quizid = ?
                AND qv.version = (SELECT MAX(v.version) FROM {question_versions} v WHERE v.questionbankentryid = qbe.id)
           ORDER BY slot.slot",
            [$quizid]
        );
        $ids = [];
        foreach ($rows as $row) {
            $ids[(int) $row->questionid] = (int) $row->slot;
        }
        return $ids;
    }

    private static function module_id(string $name): int {
        global $DB;
        return (int) $DB->get_field('modules', 'id', ['name' => $name], MUST_EXIST);
    }
}
