<?php
// Export the course structure the learner simulator needs.
//
// Usage: php export_structure.php <course shortname>
//
// quizzes   every quiz in course order: its cmid, pass mark, role (course-module
//           ID number prefix, layout kc), its fixed slots' question-bank
//           entries and KCs (ID number of the category), and its random slots
//           with the KC and the candidate entries their filter allows
//           (category + all its tags, latest ready version)
// items     entry id -> kc_id and the authored labels from question tags
//           (difficulty, cefr, item_set); empty for an untagged course
// lessons   kc_id -> cmid of the KC's lesson page: the page linked to the KC's
//           competency (competency ID number = kc_id), or, in a course
//           without module competencies, the page whose ID number is the KC
// overviews section number -> cmid of the first other page in the section
//           (the unit overview)

require(__DIR__ . '/lib.php');

$course = setup_get_course($argv[1] ?? '');
$modinfo = get_fast_modinfo($course);
$quizzes = $lessons = $overviews = [];
// cmid -> ID numbers of the competencies linked to the module.
$modulekcs = [];
$links = $DB->get_recordset_sql(
    "SELECT mc.id, mc.cmid, c.idnumber
       FROM {competency_modulecomp} mc
       JOIN {competency} c ON c.id = mc.competencyid
       JOIN {course_modules} cm ON cm.id = mc.cmid
      WHERE cm.course = ?
   ORDER BY mc.cmid, mc.sortorder, mc.id",
    [$course->id]
);
foreach ($links as $link) {
    $modulekcs[(int) $link->cmid][] = $link->idnumber;
}
$links->close();
$usecompetencies = !empty($modulekcs);

// Every question-bank entry of the course's categories with its KC and the
// tags of its latest version: the candidates of random slots and the labels.
const ROLE_PREFIXES = ['CHECK' => 'check', 'PRACTICE' => 'practice', 'CHECKIN' => 'checkin',
    'REVIEW' => 'review', 'CHALLENGE' => 'challenge'];
$bankentries = [];   // entry id -> ['category' => id, 'kc_id' => ..., 'tagids' => [], 'tags' => []]
$rows = $DB->get_recordset_sql(
    "SELECT qbe.id AS entryid, qbe.questioncategoryid AS category, qc.idnumber AS kc_id,
            qv.questionid, qv.status
       FROM {question_bank_entries} qbe
       JOIN {question_categories} qc ON qc.id = qbe.questioncategoryid
       JOIN {question_versions} qv ON qv.questionbankentryid = qbe.id
      WHERE qv.version = (SELECT MAX(v2.version) FROM {question_versions} v2
                           WHERE v2.questionbankentryid = qbe.id)
        AND qc.contextid IN (SELECT ctx.id FROM {context} ctx
                               JOIN {course_modules} cm ON cm.id = ctx.instanceid
                              WHERE ctx.contextlevel = " . CONTEXT_MODULE . " AND cm.course = ?)",
    [$course->id]
);
$entryofquestion = [];
foreach ($rows as $row) {
    if ($row->status !== \core_question\local\bank\question_version_status::QUESTION_STATUS_READY) {
        continue;
    }
    $bankentries[(int) $row->entryid] = ['category' => (int) $row->category, 'kc_id' => (string) $row->kc_id,
        'tagids' => [], 'tags' => []];
    $entryofquestion[(int) $row->questionid] = (int) $row->entryid;
}
$rows->close();
if ($entryofquestion) {
    [$insql, $params] = $DB->get_in_or_equal(array_keys($entryofquestion));
    $tagrows = $DB->get_records_sql(
        "SELECT ti.id, ti.itemid, t.id AS tagid, t.name
           FROM {tag_instance} ti
           JOIN {tag} t ON t.id = ti.tagid
          WHERE ti.component = 'core_question' AND ti.itemtype = 'question' AND ti.itemid $insql",
        $params
    );
    foreach ($tagrows as $tag) {
        $entry = $entryofquestion[(int) $tag->itemid];
        $bankentries[$entry]['tagids'][] = (int) $tag->tagid;
        $bankentries[$entry]['tags'][] = core_text::strtolower($tag->name);
    }
}
$items = [];
foreach ($bankentries as $entryid => $entry) {
    $label = ['kc_id' => $entry['kc_id'], 'difficulty' => '', 'cefr' => '', 'item_set' => ''];
    foreach ($entry['tags'] as $tag) {
        if (preg_match('/^ml-difficulty-([1-9])$/', $tag, $m)) {
            $label['difficulty'] = $m[1];
        } else if (preg_match('/^ml-cefr-([a-c][12])$/', $tag, $m)) {
            $label['cefr'] = strtoupper($m[1]);
        } else if ($tag === 'ml-check' || $tag === 'ml-pool') {
            $label['item_set'] = substr($tag, 3);
        }
    }
    if ($entry['tags']) {
        $items[(string) $entryid] = $label;
    }
}
foreach ($modinfo->get_sections() as $sectionnumber => $cmids) {
    foreach ($cmids as $cmid) {
        $cm = $modinfo->get_cm($cmid);
        if ($cm->modname === 'page') {
            $kcid = $usecompetencies
                ? (count($modulekcs[(int) $cm->id] ?? []) === 1 ? $modulekcs[(int) $cm->id][0] : null)
                : (($cm->idnumber !== null && $cm->idnumber !== '') ? $cm->idnumber : null);
            if ($kcid !== null) {
                $lessons[$kcid] = $lessons[$kcid] ?? (int) $cm->id;   // First in course order.
            } else if (!isset($overviews[$sectionnumber])) {
                $overviews[$sectionnumber] = (int) $cm->id;
            }
            continue;
        }
        if ($cm->modname !== 'quiz') {
            continue;
        }
        $slots = $DB->get_records_sql(
            "SELECT qs.slot, qr.questionbankentryid, qc.idnumber AS kc_id
               FROM {quiz_slots} qs
               JOIN {question_references} qr
                 ON qr.component = 'mod_quiz' AND qr.questionarea = 'slot' AND qr.itemid = qs.id
               JOIN {question_bank_entries} qbe ON qbe.id = qr.questionbankentryid
               JOIN {question_categories} qc ON qc.id = qbe.questioncategoryid
              WHERE qs.quizid = ?
           ORDER BY qs.slot",
            [$cm->instance]
        );
        $entries = [];
        foreach ($slots as $slot) {
            if ($slot->kc_id === null || $slot->kc_id === '') {
                cli_error("Quiz {$cm->name} uses a question whose category has no ID number (no KC).");
            }
            $entries[] = ['entry_id' => (int) $slot->questionbankentryid, 'kc_id' => $slot->kc_id];
        }
        // Random slots: the category and the tags (all of them) of the filter.
        $randomrows = $DB->get_records_sql(
            "SELECT qs.slot, qsr.filtercondition
               FROM {quiz_slots} qs
               JOIN {question_set_references} qsr
                 ON qsr.component = 'mod_quiz' AND qsr.questionarea = 'slot' AND qsr.itemid = qs.id
              WHERE qs.quizid = ?
           ORDER BY qs.slot",
            [$cm->instance]
        );
        $randomslots = [];
        foreach ($randomrows as $random) {
            $filter = json_decode($random->filtercondition, true)['filter'] ?? [];
            $categoryid = (int) ($filter['category']['values'][0] ?? 0);
            $tagids = array_map('intval', $filter['qtagids']['values'] ?? []);
            $kcid = (string) $DB->get_field('question_categories', 'idnumber', ['id' => $categoryid]);
            if ($kcid === '') {
                cli_error("Quiz {$cm->name} has a random slot on a category without an ID number (no KC).");
            }
            $candidates = [];
            foreach ($bankentries as $entryid => $entry) {
                if ($entry['category'] === $categoryid && !array_diff($tagids, $entry['tagids'])) {
                    $candidates[] = $entryid;
                }
            }
            sort($candidates);
            if (!$candidates) {
                cli_error("Quiz {$cm->name} slot {$random->slot}: no question matches its filter.");
            }
            $randomslots[] = ['slot' => (int) $random->slot, 'kc_id' => $kcid, 'candidates' => $candidates];
        }
        if (!$entries && !$randomslots) {
            continue;
        }
        $prefix = strtoupper(explode('-', (string) $cm->idnumber, 2)[0]);
        $role = ROLE_PREFIXES[$prefix] ?? 'check';
        $quiz = $DB->get_record('quiz', ['id' => $cm->instance], 'id, grade, sumgrades', MUST_EXIST);
        $gradeitem = grade_item::fetch([
            'itemtype' => 'mod', 'itemmodule' => 'quiz', 'iteminstance' => $cm->instance,
            'itemnumber' => 0, 'courseid' => $course->id,
        ]);
        $quizzes[] = [
            'quiz_id' => (int) $cm->instance,
            'cmid' => (int) $cm->id,
            'name' => $cm->name,
            'section' => (int) $sectionnumber,
            'grade' => (float) $quiz->grade,
            'gradepass' => $gradeitem ? (float) $gradeitem->gradepass : 0.0,
            'completion_pass_required' => (int) $cm->completionpassgrade,
            'idnumber' => (string) $cm->idnumber,
            'role' => $role,
            'entries' => $entries,
            'random_slots' => $randomslots,
        ];
    }
}
setup_result([
    'course_id' => (int) $course->id,
    'startdate' => (int) $course->startdate,
    'quizzes' => $quizzes,
    'lessons' => (object) $lessons,
    'overviews' => (object) $overviews,
    'items' => (object) $items,
]);
