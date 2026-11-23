<?php
// Build the English microlearning course from a course specification.
//
// Usage: php build_course.php <course_spec.json> [--rebuild]
//
// Creates: the course with one section per unit, one course question bank
// whose categories are  top > dimension > KC  (KC category ID number = kc_id),
// the questions, one overview page per unit and one lesson page per KC. The
// quizzes depend on the spec's layout:
//
//   kc         follows spec.sections and spec.quizzes (moodle_setup/content.py):
//              per unit an overview, a check-in, then per KC its lesson, a
//              practice quiz and a check quiz, and from unit 2 a review quiz;
//              a last section holds one challenge quiz per KC. Each quiz's
//              course-module ID number is its role (CHECK-KC001, PRACTICE-...,
//              CHECKIN-U3, REVIEW-U3, CHALLENGE-...). Check quizzes hold the
//              KC's fixed check questions and have the pass mark; the others
//              draw random questions from the KC category filtered by tags
//              (ml-pool, and ml-difficulty-3 for challenges), have no pass
//              mark and are complete once graded. Questions carry their tags
//              (item set, difficulty, CEFR). Every lesson and quiz is linked to
//              its KCs' competencies (framework spec.kc_framework_idnumber,
//              competency ID number = kc_id); lessons carry no ID number.
//   dimension  one practice quiz per unit and dimension (~3 KCs); the lesson's
//              course-module ID number = kc_id. No competencies, no tags.
//              (Course 10.)
//
// The category layout and the module competencies (or lesson ID numbers) are
// the conventions the collector reads to derive the KC catalog, the
// question-to-KC mapping and the KC-to-activity map.
//
// Activity completion: pages are complete when viewed; a quiz is complete
// with a passing grade (pass_fraction of the marks), so a failed quiz is
// recorded as complete_fail.

require(__DIR__ . '/lib.php');

$spec = setup_read_json($argv[1] ?? '');
$rebuild = in_array('--rebuild', $argv, true);
$layout = $spec['layout'] ?? 'dimension';
if (!in_array($layout, ['kc', 'dimension'], true)) {
    cli_error("Unknown course layout {$layout}.");
}
$usecompetencies = $layout === 'kc';

if (empty($CFG->enablecompletion)) {
    cli_error('Completion tracking is switched off for the whole site. Turn on "Enable completion '
        . 'tracking" in Site administration > Advanced features, then run this again.');
}
if ($usecompetencies && !\core_tag_tag::is_enabled('core_question', 'question')) {
    cli_error('Tags are switched off for questions. Turn on "Enable tags" in Site administration > '
        . 'Advanced features (and the question tag area), then run this again.');
}
if ($usecompetencies && empty($spec['quizzes'])) {
    cli_error('The kc layout needs spec.quizzes and spec.sections; regenerate the spec with moodle_setup.');
}
if ($usecompetencies && !\core_competency\api::is_enabled()) {
    cli_error('Competencies are switched off for the whole site. Turn on "Enable competencies" in '
        . 'Site administration > Advanced features, then run this again.');
}

$existing = $DB->get_record('course', ['shortname' => $spec['shortname']]);
if ($existing) {
    if (!$rebuild) {
        cli_error("Course {$spec['shortname']} already exists (id {$existing->id}). "
            . "Use --rebuild to delete and recreate it, including all attempts in it.");
    }
    cli_writeln("Deleting existing course {$spec['shortname']} (id {$existing->id})...");
    delete_course($existing, false);
}

$generator = \core\test\phpunit\phpunit_util::get_data_generator();
$questiongenerator = $generator->get_plugin_generator('core_question');

// ---- course and sections ---------------------------------------------------
// The start date lies in the past so simulated learners can have a history.
$startdate = usergetmidnight(time() - 150 * DAYSECS);
// Layout kc adds a section after the units (challenge practice).
$sectionspecs = $spec['sections'] ?? array_map(
    fn($unit) => ['number' => $unit['number'], 'name' => $unit['name']], $spec['units']);
$course = create_course((object) [
    'fullname' => $spec['fullname'],
    'shortname' => $spec['shortname'],
    'category' => \core_course_category::get_default()->id,
    'format' => 'topics',
    'numsections' => count($sectionspecs),
    'startdate' => $startdate,
    'enablecompletion' => 1,
    'summary' => $usecompetencies
        ? 'Microlearning for everyday English: each unit starts with a short check-in; every point '
            . 'has a short lesson, practice questions and a 4-5 question check.'
        : 'Short practice activities for everyday English: grammar, vocabulary, reading and writing.',
    'summaryformat' => FORMAT_HTML,
]);
course_create_sections_if_missing($course, range(0, count($sectionspecs)));
$unitnumbers = array_column($spec['units'], 'number');
foreach ($sectionspecs as $sectionspec) {
    $section = $DB->get_record('course_sections', ['course' => $course->id, 'section' => $sectionspec['number']], '*', MUST_EXIST);
    $name = in_array($sectionspec['number'], $unitnumbers, true)
        ? "Unit {$sectionspec['number']}: {$sectionspec['name']}"
        : $sectionspec['name'];
    course_update_section($course, $section, ['name' => $name]);
}
cli_writeln("Created course {$course->shortname} (id {$course->id}) with " . count($spec['units']) . " units.");

// ---- competencies: one per KC (layout kc) -----------------------------------
// The framework is site-wide and survives a course rebuild, so it is reused:
// existing competencies are updated, missing ones created. Dimension
// competencies are parents, mirroring the question categories.
$competencyofkc = [];
if ($usecompetencies) {
    $competencyofkc = setup_kc_competencies($spec);
    foreach ($competencyofkc as $competencyid) {
        \core_competency\api::add_competency_to_course($course->id, $competencyid);
    }
    cli_writeln('Linked ' . count($competencyofkc) . " KC competencies (framework {$spec['kc_framework_idnumber']}) to the course.");
}

/**
 * Find or create the KC framework and its competencies; return kc_id => competency id.
 */
function setup_kc_competencies(array $spec): array {
    global $DB;
    $idnumber = $spec['kc_framework_idnumber'];
    $framework = \core_competency\competency_framework::get_record(['idnumber' => $idnumber]);
    if (!$framework) {
        $scale = $DB->get_record_select('scale', 'courseid = 0 AND ' . $DB->sql_compare_text('name') . ' = ?',
            [get_string('defaultcompetencescale')], 'id, scale', IGNORE_MULTIPLE);
        if (!$scale || count(explode(',', $scale->scale)) !== 2) {
            cli_error('The site has no two-item "' . get_string('defaultcompetencescale') . '" scale for the KC framework.');
        }
        // Item 1 "not yet competent" is the default, item 2 "competent" is proficient.
        $framework = \core_competency\api::create_framework((object) [
            'shortname' => 'English Microlearning KCs',
            'idnumber' => $idnumber,
            'description' => 'One competency per Knowledge Component (ID number = kc_id), '
                . 'grouped by dimension. Created by moodle_setup build-course.',
            'descriptionformat' => FORMAT_HTML,
            'visible' => 1,
            'contextid' => \context_system::instance()->id,
            'scaleid' => $scale->id,
            'scaleconfiguration' => json_encode([
                ['scaleid' => $scale->id],
                ['id' => 1, 'scaledefault' => 1, 'proficient' => 0],
                ['id' => 2, 'scaledefault' => 0, 'proficient' => 1],
            ]),
        ]);
        cli_writeln("Created competency framework {$idnumber}.");
    }
    $frameworkid = $framework->get('id');
    $upsert = function (string $compidnumber, string $shortname, string $description, int $parentid)
            use ($frameworkid) {
        $existing = \core_competency\competency::get_record(
            ['competencyframeworkid' => $frameworkid, 'idnumber' => $compidnumber]);
        if ($existing) {
            if ($existing->get('shortname') !== $shortname || $existing->get('description') !== $description) {
                $existing->set('shortname', $shortname);
                $existing->set('description', $description);
                $existing->update();
            }
            return (int) $existing->get('id');
        }
        return (int) \core_competency\api::create_competency((object) [
            'competencyframeworkid' => $frameworkid,
            'idnumber' => $compidnumber,
            'shortname' => $shortname,
            'description' => $description,
            'descriptionformat' => FORMAT_HTML,
            'parentid' => $parentid,
        ])->get('id');
    };
    $dimensionids = [];
    foreach ($spec['dimensions'] as $dimension) {
        $dimensionids[$dimension] = $upsert('DIM-' . $dimension, $dimension,
            "Knowledge Components of the {$dimension} dimension.", 0);
    }
    $ids = [];
    foreach ($spec['kcs'] as $kc) {
        $ids[$kc['kc_id']] = $upsert($kc['kc_id'], $kc['name'], $kc['objective'],
            $dimensionids[$kc['dimension']]);
    }
    return $ids;
}

// ---- question bank: top > dimension > KC ------------------------------------
$qbank = $generator->create_module('qbank', [
    'course' => $course->id,
    'name' => 'English Microlearning question bank',
    'section' => 0,
]);
$bankcontext = \context_module::instance($qbank->cmid);
$top = question_get_top_category($bankcontext->id, true);

$dimensioncategories = [];
foreach ($spec['dimensions'] as $dimension) {
    $dimensioncategories[$dimension] = $questiongenerator->create_question_category([
        'contextid' => $bankcontext->id,
        'parent' => $top->id,
        'name' => $dimension,
        'info' => "Knowledge Components of the {$dimension} dimension.",
    ]);
}
$kccategories = [];
foreach ($spec['kcs'] as $kc) {
    $kccategories[$kc['kc_id']] = $questiongenerator->create_question_category([
        'contextid' => $bankcontext->id,
        'parent' => $dimensioncategories[$kc['dimension']]->id,
        'name' => $kc['name'],
        'info' => $kc['objective'],
        'idnumber' => $kc['kc_id'],
    ]);
}
cli_writeln('Created ' . count($kccategories) . ' KC categories under ' . count($dimensioncategories) . ' dimensions.');

// ---- questions --------------------------------------------------------------
$kcs = array_column($spec['kcs'], null, 'kc_id');
$questionsbykc = [];
$questionidbyname = [];
foreach ($spec['questions'] as $item) {
    $answers = $fractions = $feedback = [];
    foreach ($item['options'] as $index => $option) {
        $answers[] = ['text' => s($option), 'format' => FORMAT_HTML];
        $fractions[] = $index === 0 ? '1.0' : '0.0';   // The first option is the correct one.
        $feedback[] = ['text' => '', 'format' => FORMAT_HTML];
    }
    // Saved through the question type itself (the core question generator
    // needs PHPUnit, which a normal site does not have).
    $form = (object) [
        'category' => $kccategories[$item['kc_id']]->id . ',' . $bankcontext->id,
        'name' => $item['name'],
        'questiontext' => ['text' => '<p>' . s($item['stem']) . '</p>', 'format' => FORMAT_HTML],
        'generalfeedback' => [
            'text' => '<p>Focus of this question: ' . s($kcs[$item['kc_id']]['objective']) . '</p>',
            'format' => FORMAT_HTML,
        ],
        'defaultmark' => 1,
        'penalty' => 0.3333333,
        'status' => \core_question\local\bank\question_version_status::QUESTION_STATUS_READY,
        'idnumber' => null,
        'single' => '1',
        'shuffleanswers' => 1,
        'answernumbering' => 'abc',
        'showstandardinstruction' => 0,
        'shownumcorrect' => 0,
        'correctfeedback' => ['text' => 'Your answer is correct.', 'format' => FORMAT_HTML],
        'partiallycorrectfeedback' => ['text' => 'Your answer is partially correct.', 'format' => FORMAT_HTML],
        'incorrectfeedback' => ['text' => 'Your answer is incorrect.', 'format' => FORMAT_HTML],
        'noanswers' => count($answers),
        'answer' => $answers,
        'fraction' => $fractions,
        'feedback' => $feedback,
    ];
    $question = (object) [
        'qtype' => 'multichoice',
        'createdby' => $USER->id,
        'idnumber' => null,
        'status' => $form->status,
    ];
    $question = question_bank::get_qtype('multichoice')->save_question($question, $form);
    $questionsbykc[$item['kc_id']][] = $question->id;
    $questionidbyname[$item['name']] = $question->id;
    if (!empty($item['tags'])) {
        // Item set, difficulty and CEFR level (layout kc); random slots filter on them.
        \core_tag_tag::set_item_tags('core_question', 'question', $question->id, $bankcontext, $item['tags']);
    }
}
cli_writeln('Created ' . count($spec['questions']) . ' questions.');

// ---- per unit: overview page, then lessons and quizzes -----------------------
// Page completion: complete once viewed. Quiz completion: complete with a
// passing grade, so Moodle records complete_pass / complete_fail.
$viewcompletion = ['completion' => COMPLETION_TRACKING_AUTOMATIC, 'completionview' => COMPLETION_VIEW_REQUIRED];
$quizgrade = 10;
$gradepass = round($quizgrade * (float) $spec['pass_fraction'], 2);
$lessons = array_column($spec['lessons'], null, 'kc_id');
$quizcount = $lessoncount = $modulelinks = 0;

/** Create a practice quiz holding the questions of $kcids. */
$makequiz = function (int $section, string $name, string $intro, array $kcids) use (
        $course, $generator, $DB, $quizgrade, $gradepass, $questionsbykc) {
    $quiz = $generator->create_module('quiz', [
        'course' => $course->id,
        'section' => $section,
        'name' => $name,
        'intro' => $intro,
        'preferredbehaviour' => 'immediatefeedback',
        'attempts' => 0,
        'questionsperpage' => 1,
        'shuffleanswers' => 1,
        'grade' => $quizgrade,
        'gradepass' => $gradepass,
        'timeopen' => 0,
        'timeclose' => 0,
        'timelimit' => 0,
        'completion' => COMPLETION_TRACKING_AUTOMATIC,
        'completionusegrade' => 1,
        'completionpassgrade' => 1,
    ]);
    $quizrecord = $DB->get_record('quiz', ['id' => $quiz->id], '*', MUST_EXIST);
    foreach ($kcids as $kcid) {
        foreach ($questionsbykc[$kcid] as $questionid) {
            quiz_add_quiz_question($questionid, $quizrecord, 0, 1);
        }
    }
    \mod_quiz\quiz_settings::create($quiz->id)->get_grade_calculator()->recompute_quiz_sumgrades();
    return $quiz;
};

foreach ($usecompetencies ? [] : $spec['units'] as $unit) {
    $unitkcs = array_filter($spec['kcs'], fn($kc) => $kc['unit'] === $unit['number']);
    $objectives = '';
    foreach ($spec['dimensions'] as $dimension) {
        $items = array_filter($unitkcs, fn($kc) => $kc['dimension'] === $dimension);
        if (!$items) {
            continue;
        }
        $objectives .= '<h4>' . s($dimension) . '</h4><ul>';
        foreach ($items as $kc) {
            $objectives .= '<li><strong>' . s($kc['name']) . '</strong>: ' . s($kc['objective']) . '</li>';
        }
        $objectives .= '</ul>';
    }
    $howitworks = $usecompetencies
        ? 'Each point has a short lesson, followed by a short check (4-5 questions) on that point only. '
        : 'Each point has a short lesson, followed by a practice quiz for its skill area. ';
    $generator->create_module('page', [
        'course' => $course->id,
        'section' => $unit['number'],
        'name' => "Unit {$unit['number']} overview: {$unit['name']}",
        'intro' => 'What you will practise in this unit.',
        'content' => '<p>In this unit you will practise:</p>' . $objectives
            . '<p>' . $howitworks . 'A '
            . ($usecompetencies ? 'check' : 'practice quiz') . ' counts as complete when you reach '
            . s((string) $gradepass) . '/' . $quizgrade . '.</p>',
        'contentformat' => FORMAT_HTML,
    ] + $viewcompletion);

    foreach ($spec['dimensions'] as $dimension) {
        $items = array_filter($unitkcs, fn($kc) => $kc['dimension'] === $dimension);
        if (!$items) {
            continue;
        }
        foreach ($items as $kc) {
            $lesson = $generator->create_module('page', [
                'course' => $course->id,
                'section' => $unit['number'],
                'name' => "Lesson: {$kc['name']}",
                // Layout dimension: the ID number names the KC (collector convention).
                // Layout kc: the competency does, so the ID number stays empty.
                'idnumber' => $usecompetencies ? '' : $kc['kc_id'],
                'intro' => s($kc['objective']),
                'content' => $lessons[$kc['kc_id']]['html'],
                'contentformat' => FORMAT_HTML,
            ] + $viewcompletion);
            $lessoncount++;
        }
        $makequiz($unit['number'], "Unit {$unit['number']} {$dimension} practice",
            "Short {$dimension} practice for {$unit['name']}. You can try as often as you like; "
            . "it counts as complete when you reach {$gradepass}/{$quizgrade}.",
            array_column($items, 'kc_id'));
        $quizcount++;
    }
}
// ---- layout kc: sections, lessons and role quizzes from the spec ---------------
$rolecount = [];
$randomslotcount = 0;
if ($usecompetencies) {
    $tagcollid = \core_tag_area::get_collection('core_question', 'question');
    $tagids = [];
    $tagid = function (string $name) use (&$tagids, $tagcollid): int {
        if (!isset($tagids[$name])) {
            $tag = \core_tag_tag::get_by_name($tagcollid, $name, 'id');
            if (!$tag) {
                cli_error("Question tag {$name} does not exist; were the questions tagged?");
            }
            $tagids[$name] = (int) $tag->id;
        }
        return $tagids[$name];
    };
    $intros = [
        'checkin' => 'Before the lessons: one question on each point of this unit, so your '
            . 'recommendations can start from what you already know. One try; it is not a test.',
        'practice' => 'Practice on this point: a few questions from a larger set, as often as you like. '
            . 'Any attempt counts as practice.',
        'check' => 'A short check on this point only. You can try as often as you like; '
            . "it counts as complete when you reach {$gradepass}/{$quizgrade}.",
        'review' => 'Review of earlier units: one question on each of a few earlier points. '
            . 'Try it as often as you like.',
        'challenge' => 'Harder questions on this point, for when you have passed its check.',
    ];

    /** Create one quiz of the spec: fixed questions or random pool slots. */
    $makerolequiz = function (int $section, array $q) use (
            $course, $generator, $DB, $quizgrade, $questionidbyname, $kccategories, $bankcontext,
            $tagid, $intros, &$randomslotcount) {
        $passgrade = $q['pass_fraction'] ? round($quizgrade * (float) $q['pass_fraction'], 2) : 0;
        $quiz = $generator->create_module('quiz', [
            'course' => $course->id,
            'section' => $section,
            'name' => $q['name'],
            'idnumber' => $q['idnumber'],
            'intro' => $intros[$q['role']],
            'preferredbehaviour' => 'immediatefeedback',
            'attempts' => (int) $q['max_attempts'],
            'questionsperpage' => 1,
            'shuffleanswers' => 1,
            'grade' => $quizgrade,
            'gradepass' => $passgrade,
            'timeopen' => 0,
            'timeclose' => 0,
            'timelimit' => 0,
            'completion' => COMPLETION_TRACKING_AUTOMATIC,
            'completionusegrade' => 1,
            'completionpassgrade' => $passgrade > 0 ? 1 : 0,
        ]);
        $quizrecord = $DB->get_record('quiz', ['id' => $quiz->id], '*', MUST_EXIST);
        foreach ($q['questions'] as $name) {
            quiz_add_quiz_question($questionidbyname[$name], $quizrecord, 0, 1);
        }
        if ($q['slots']) {
            $structure = \mod_quiz\quiz_settings::create($quiz->id)->get_structure();
            foreach ($q['slots'] as $index => $slot) {
                $categoryid = (int) $kccategories[$slot['kc_id']]->id;
                $structure->add_random_questions($index + 1, 1, [
                    'cat' => "{$categoryid},{$bankcontext->id}",
                    'filter' => [
                        'category' => [
                            'jointype' => 1,
                            'values' => [$categoryid],
                            'filteroptions' => ['includesubcategories' => false],
                        ],
                        // Every listed tag (JOINTYPE_ALL): pool, and hard for challenges.
                        'qtagids' => [
                            'jointype' => 2,
                            'values' => array_map($tagid, $slot['tags']),
                        ],
                    ],
                ]);
                $randomslotcount++;
            }
        }
        \mod_quiz\quiz_settings::create($quiz->id)->get_grade_calculator()->recompute_quiz_sumgrades();
        return $quiz;
    };

    $quizspecs = array_column($spec['quizzes'], null, 'idnumber');
    foreach ($spec['sections'] as $sectionspec) {
        $number = (int) $sectionspec['number'];
        foreach ($sectionspec['items'] as $item) {
            if ($item['type'] === 'overview') {
                $unitkcs = array_filter($spec['kcs'], fn($kc) => $kc['unit'] === $number);
                $objectives = '';
                foreach ($spec['dimensions'] as $dimension) {
                    $items = array_filter($unitkcs, fn($kc) => $kc['dimension'] === $dimension);
                    if (!$items) {
                        continue;
                    }
                    $objectives .= '<h4>' . s($dimension) . '</h4><ul>';
                    foreach ($items as $kc) {
                        $objectives .= '<li><strong>' . s($kc['name']) . '</strong> (' . s($kc['cefr'])
                            . '): ' . s($kc['objective']) . '</li>';
                    }
                    $objectives .= '</ul>';
                }
                $generator->create_module('page', [
                    'course' => $course->id,
                    'section' => $number,
                    'name' => "Unit {$number} overview: {$sectionspec['name']}",
                    'intro' => 'What you will practise in this unit.',
                    'content' => '<p>In this unit you will practise:</p>' . $objectives
                        . '<p>Start with the short check-in. Then each point has a short lesson, '
                        . 'practice questions and a check, which counts as complete when you reach '
                        . s((string) $gradepass) . '/' . $quizgrade . '.</p>',
                    'contentformat' => FORMAT_HTML,
                ] + $viewcompletion);
            } else if ($item['type'] === 'lesson') {
                $kc = $kcs[$item['kc_id']];
                $lesson = $generator->create_module('page', [
                    'course' => $course->id,
                    'section' => $number,
                    'name' => "Lesson: {$kc['name']}",
                    'idnumber' => '',   // The competency names the KC.
                    'intro' => s($kc['objective']),
                    'content' => $lessons[$kc['kc_id']]['html'],
                    'contentformat' => FORMAT_HTML,
                ] + $viewcompletion);
                $lessoncount++;
                \core_competency\api::add_competency_to_course_module($lesson->cmid, $competencyofkc[$kc['kc_id']]);
                $modulelinks++;
            } else {
                $q = $quizspecs[$item['idnumber']];
                $quiz = $makerolequiz($number, $q);
                $quizcount++;
                $rolecount[$q['role']] = ($rolecount[$q['role']] ?? 0) + 1;
                foreach (array_unique($q['kc_ids']) as $kcid) {
                    \core_competency\api::add_competency_to_course_module($quiz->cmid, $competencyofkc[$kcid]);
                    $modulelinks++;
                }
            }
        }
    }
    ksort($rolecount);
    cli_writeln('Quizzes by role: ' . json_encode($rolecount) . "; {$randomslotcount} random slots.");
}

// ---- recommendations block ---------------------------------------------------
// Every rebuild creates a new course, so the student block is added here
// rather than by hand. Skipped when the plugin is not installed.
$blockadded = false;
if (array_key_exists('microlearning', \core_component::get_plugin_list('block'))
        && $DB->record_exists('block', ['name' => 'microlearning', 'visible' => 1])) {
    $coursecontext = \context_course::instance($course->id);
    $blockpage = new \moodle_page();
    $blockpage->set_context($coursecontext);
    $blockpage->set_course($course);
    $blockpage->set_pagelayout('course');
    $blockpage->set_pagetype('course-view-topics');
    $blockpage->blocks->add_region('side-pre', false);
    // Shown on the course page and on its activity pages.
    $blockpage->blocks->add_block('microlearning', 'side-pre', 0, true, '*');
    $blockadded = true;
}

rebuild_course_cache($course->id, true);
cli_writeln($blockadded
    ? 'Added the "Recommended for you" block to the course.'
    : 'Block block_microlearning is not installed; no recommendations block was added.');
cli_writeln("Created {$lessoncount} KC lessons, {$quizcount} quizzes and "
    . count($spec['units']) . ' overview pages, all with completion tracking'
    . ($usecompetencies ? "; {$modulelinks} activity-competency links." : '.'));

setup_result([
    'course_id' => (int) $course->id,
    'shortname' => $course->shortname,
    'startdate' => (int) $course->startdate,
    'kcs' => count($kccategories),
    'questions' => count($spec['questions']),
    'quizzes' => $quizcount,
    'lessons' => $lessoncount,
    'gradepass' => $gradepass,
    'layout' => $layout,
    'competencies' => count($competencyofkc),
    'competency_links' => $modulelinks,
    'quizzes_by_role' => (object) $rolecount,
    'random_slots' => $randomslotcount,
]);
