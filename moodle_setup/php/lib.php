<?php
// Shared bootstrap for the moodle_setup CLI scripts.
//
// These scripts run inside the Moodle web container (see ../docker.py). They
// only use Moodle's own APIs, so everything they create is ordinary Moodle
// data: visible in the UI, exported by the collector, removable in the UI.

define('CLI_SCRIPT', true);
require('/var/www/html/config.php');
require_once($CFG->libdir . '/clilib.php');
require_once($CFG->libdir . '/questionlib.php');
require_once($CFG->dirroot . '/course/lib.php');
require_once($CFG->dirroot . '/user/lib.php');
require_once($CFG->dirroot . '/mod/quiz/locallib.php');
require_once($CFG->dirroot . '/mod/page/lib.php');
require_once($CFG->libdir . '/completionlib.php');
require_once($CFG->libdir . '/gradelib.php');

// ID-number prefix that marks an account as a simulated learner. The collector
// uses the same prefix to label exports as simulated.
const SIM_IDNUMBER_PREFIX = 'SIM-';
const RESULT_MARKER = 'MOODLE_SETUP_RESULT:';

\core\session\manager::set_user(get_admin());

/** Report the script result to the Python driver. */
function setup_result(array $data): void {
    echo RESULT_MARKER . json_encode($data) . "\n";
}

/** Read a JSON input file passed on the command line. */
function setup_read_json(string $path): array {
    if (!is_readable($path)) {
        cli_error("Cannot read input file: {$path}");
    }
    $data = json_decode(file_get_contents($path), true);
    if (!is_array($data)) {
        cli_error("Input file is not valid JSON: {$path}");
    }
    return $data;
}

/** The course built by build_course.php, or an error if it does not exist. */
function setup_get_course(string $shortname): stdClass {
    global $DB;
    $course = $DB->get_record('course', ['shortname' => $shortname]);
    if (!$course) {
        cli_error("Course {$shortname} does not exist. Run: python -m moodle_setup build-course");
    }
    return $course;
}

/** All simulated learner accounts. */
function setup_simulated_users(): array {
    global $DB;
    return $DB->get_records_select(
        'user',
        'deleted = 0 AND ' . $DB->sql_like('idnumber', ':prefix'),
        ['prefix' => SIM_IDNUMBER_PREFIX . '%'],
        'idnumber'
    );
}

/**
 * Start a quiz attempt with the planned question in every random slot.
 *
 * Simulated learners only. Core quiz_start_new_attempt() accepts forced
 * question ids, but looks them up by $quba->next_slot_number(), which stays 1
 * while it fills the random slots (Moodle 5.2), so it can force one random
 * slot only. This follows the same steps as core: fixed slots load their
 * question, each random slot loads the planned one after core's own filter
 * check (random_question_loader::is_filtered_question_available, category
 * and tags), the usage is started with the least-used variant strategy, and
 * the layout is built from the quiz sections. Fails loudly instead of
 * substituting another question.
 *
 * @param \mod_quiz\quiz_settings $quizobj
 * @param question_usage_by_activity $quba
 * @param stdClass $attempt from quiz_create_attempt()
 * @param array $questionidbyslot slot number => question id (random slots only)
 * @return stdClass the attempt with its layout
 */
function setup_start_attempt_with_questions($quizobj, $quba, stdClass $attempt, array $questionidbyslot): stdClass {
    $qubaids = new \mod_quiz\question\qubaids_for_users_attempts($quizobj->get_quizid(), $attempt->userid);
    $quizobj->preload_questions();
    $loader = new \core_question\local\bank\random_question_loader($qubaids, []);
    $shuffle = $quizobj->get_quiz()->shuffleanswers;
    $questions = $maxmark = $page = [];
    $slot = 0;
    foreach ($quizobj->get_questions(null, false) as $questiondata) {
        $slot += 1;
        $maxmark[$slot] = $questiondata->maxmark;
        $page[$slot] = $questiondata->page;
        if (!$questiondata->random) {
            $questions[$slot] = question_bank::load_question($questiondata->questionid, $shuffle);
            continue;
        }
        if (!isset($questionidbyslot[$slot])) {
            cli_error("Quiz {$quizobj->get_quizid()} slot {$slot} is random but the plan names no question for it.");
        }
        $filters = $questiondata->filtercondition['filter'] ?? [];
        if (!$loader->is_filtered_question_available($filters, (int) $questionidbyslot[$slot])) {
            cli_error("Quiz {$quizobj->get_quizid()} slot {$slot}: planned question {$questionidbyslot[$slot]} "
                . 'does not match the slot filter; run simulate-learners again.');
        }
        $questions[$slot] = question_bank::load_question((int) $questionidbyslot[$slot], $shuffle);
    }
    ksort($questions);
    foreach ($questions as $slotnumber => $question) {
        if ($quba->add_question($question, $maxmark[$slotnumber]) != $slotnumber) {
            throw new coding_exception('Slot numbers have got confused.');
        }
    }
    $quba->start_all_questions(
        new \core_question\engine\variants\least_used_strategy($quba, $qubaids),
        question_attempt_step::TIMECREATED_ON_FIRST_RENDER,
        $attempt->userid
    );
    // Layout as in core: one entry per slot, 0 = page break, sections end a page.
    $sections = array_values($quizobj->get_sections());
    $layout = [];
    foreach ($sections as $index => $section) {
        if (!empty($section->shufflequestions)) {
            cli_error("Quiz {$quizobj->get_quizid()} shuffles questions; the simulated replay does not support that.");
        }
        $last = isset($sections[$index + 1]) ? $sections[$index + 1]->firstslot - 1 : count($questions);
        $currentpage = $page[$section->firstslot] ?? null;
        for ($s = $section->firstslot; $s <= $last; $s += 1) {
            if ($currentpage !== null && $page[$s] != $currentpage) {
                $layout[] = 0;
            }
            $layout[] = $s;
            $currentpage = $page[$s];
        }
        $layout[] = 0;
    }
    $attempt->layout = implode(',', $layout);
    return $attempt;
}
