"""Course content validation and the Moodle attempt plan (no Moodle needed)."""

import csv
import hashlib
import json
import shutil

import pytest

from knowledge_engine import config as engine_config
from knowledge_engine.phase import _simulator_ceiling_auc
from knowledge_engine.simulator import simulate_plan
from moodle_setup import __main__ as cli
from moodle_setup import content


# --- course content ------------------------------------------------------------


def test_shipped_course_content_is_valid():
    spec = content.build_spec()

    assert len(spec["units"]) == 8
    assert len(spec["kcs"]) == 96
    per_kc = {}
    for question in spec["questions"]:
        per_kc[question["kc_id"]] = per_kc.get(question["kc_id"], 0) + 1
    assert set(per_kc) == {kc["kc_id"] for kc in spec["kcs"]}
    assert min(per_kc.values()) >= content.MIN_QUESTIONS_PER_KC
    # Question names are unique within the bank.
    names = [question["name"] for question in spec["questions"]]
    assert len(names) == len(set(names))
    # Every unit has KCs in all four dimensions, so it gets four quizzes.
    combos = {(kc["unit"], kc["dimension"]) for kc in spec["kcs"]}
    assert len(combos) == 8 * len(content.DIMENSION_ORDER)


@pytest.fixture
def course_copy(tmp_path):
    target = tmp_path / "course"
    shutil.copytree(content.COURSE_DIR, target)
    return target


def _append(course_dir, line, unit="unit1.txt"):
    with (course_dir / "questions" / unit).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _problems(course_dir):
    return content.validate(content.load_catalog(course_dir), content.load_questions(course_dir))


def test_validation_rejects_unknown_kc(course_copy):
    _append(course_copy, "KC999 | 1 | Stem? | a | b | c | d")
    assert any("unknown kc_id 'KC999'" in problem for problem in _problems(course_copy))


def test_validation_rejects_repeated_options(course_copy):
    _append(course_copy, "KC001 | 1 | A brand new stem? | goes | go | Goes | gone")
    assert any("options are not distinct" in problem for problem in _problems(course_copy))


def test_validation_rejects_duplicate_questions(course_copy):
    line = 'KC001 | 1 | Choose the correct form: "My sister ___ to work by bus every day." | goes | go | going | gone'
    _append(course_copy, line)
    assert any("duplicates the question" in problem for problem in _problems(course_copy))


def test_validation_requires_enough_questions_per_kc(course_copy):
    path = course_copy / "questions" / "unit1.txt"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("KC001 ")]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert "KC001: 0 question(s), need at least 4" in _problems(course_copy)


def test_kc_layout_is_the_default_microlearning_course():
    spec = content.build_spec()

    assert spec["layout"] == "kc"
    assert spec["shortname"] == "ENG-MICRO-KC"
    assert spec["kc_framework_idnumber"] == content.KC_FRAMEWORK_IDNUMBER
    per_kc = {}
    for question in spec["questions"]:
        if question["item_set"] == content.CHECK_SET:
            per_kc[question["kc_id"]] = per_kc.get(question["kc_id"], 0) + 1
    # Each KC's check questions are its whole check quiz: 4-5 items.
    assert content.MIN_QUESTIONS_PER_KC <= min(per_kc.values())
    assert max(per_kc.values()) <= content.MAX_QUESTIONS_PER_KC


# SHA-256 of course 10's build spec (layout dimension, ``json.dumps(spec,
# sort_keys=True)``) recorded before difficulty, CEFR levels, the practice pool
# and quiz roles were added. Those are layout-kc only; course 10 must not move.
COURSE_10_SPEC_SHA256 = "1bd8a8520f84e3c02c82c075cfae3e09ff7b245238cc531aba5439a2b577913a"


def test_dimension_layout_keeps_course_10_unchanged():
    spec = content.build_spec(layout="dimension")
    kc_spec = content.build_spec(layout="kc")

    assert (spec["shortname"], spec["fullname"]) == ("ENG-MICRO", "English Microlearning")
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    assert digest == COURSE_10_SPEC_SHA256
    assert not {"quizzes", "sections", "question_tags"} & set(spec)
    # Same units, KCs and check questions; layout kc only adds fields.
    assert spec["units"] == kc_spec["units"] and spec["dimensions"] == kc_spec["dimensions"]
    core = ("kc_id", "unit", "dimension", "name", "objective")
    assert spec["kcs"] == [{key: kc[key] for key in core} for kc in kc_spec["kcs"]]
    checks = [q for q in kc_spec["questions"] if q["item_set"] == content.CHECK_SET]
    assert spec["questions"] == [
        {key: q[key] for key in ("kc_id", "name", "stem", "options")} for q in checks
    ]
    with pytest.raises(ValueError, match="Unknown course layout"):
        content.build_spec(layout="unit")


def test_kc_layout_rejects_a_check_quiz_longer_than_five_items(course_copy):
    for number in range(6):
        _append(course_copy, f"KC001 | 2 | Extra stem number {number}? | a{number} | b | c | d")
    catalog, questions = content.load_catalog(course_copy), content.load_questions(course_copy)

    kc_problems = content.validate(catalog, questions, layout="kc")
    assert any(p.startswith("KC001:") and "at most 5" in p for p in kc_problems)
    # The mixed-quiz layout has no upper bound.
    assert not any("at most" in p for p in content.validate(catalog, questions, layout="dimension"))


def test_validation_rejects_unknown_dimension(course_copy):
    path = course_copy / "kc_catalog.csv"
    with path.open(newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    rows[0]["dimension"] = "Listening"
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=content.CATALOG_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    assert any("unknown dimension 'Listening'" in problem for problem in _problems(course_copy))


def test_malformed_line_is_an_error(course_copy):
    _append(course_copy, "KC001 | 1 | Only two options? | yes | no")
    with pytest.raises(ValueError, match="expected kc_id"):
        content.build_spec(course_copy)


# --- attempt plan --------------------------------------------------------------


def _quizzes():
    """The mixed-quiz course (layout dimension) as export_structure.php would
    report it (fake entry ids)."""
    spec = content.build_spec(layout="dimension")
    unit_of = {kc["kc_id"]: (kc["unit"], content.DIMENSION_ORDER.index(kc["dimension"])) for kc in spec["kcs"]}
    grouped = {}
    for entry_id, question in enumerate(spec["questions"], start=1):
        grouped.setdefault(unit_of[question["kc_id"]], []).append(
            {"entry_id": entry_id, "kc_id": question["kc_id"]}
        )
    return [
        {"quiz_id": 100 + index, "entries": grouped[key]}
        for index, key in enumerate(sorted(grouped))
    ]


def test_plan_answers_every_question_of_each_attempt_in_time_order():
    quizzes = _quizzes()
    entries_of = {quiz["quiz_id"]: [e["entry_id"] for e in quiz["entries"]] for quiz in quizzes}
    plan, manifest = simulate_plan(quizzes, n_learners=30, seed=3, start_unix=1_700_000_000)

    assert len(plan) == 30
    assert manifest["answer_events"] == sum(len(a["answers"]) for l in plan for a in l["attempts"])
    assert 0.5 < manifest["ceiling_auc"] <= 1.0
    for learner in plan:
        assert learner["enrol_start"] >= 1_700_000_000
        previous = learner["enrol_start"]
        for attempt in learner["attempts"]:
            assert [a["entry_id"] for a in attempt["answers"]] == entries_of[attempt["quiz_id"]]
            times = [attempt["start"], *(a["time"] for a in attempt["answers"]), attempt["finish"]]
            assert times == sorted(times) and attempt["start"] > previous
            assert all(a["correct"] in (0, 1) and a["seconds"] >= 3 for a in attempt["answers"])
            previous = attempt["finish"]


def test_larger_cohort_extends_a_smaller_one():
    quizzes = _quizzes()
    small, _ = simulate_plan(quizzes, n_learners=20, seed=7, start_unix=1_700_000_000)
    large, _ = simulate_plan(quizzes, n_learners=60, seed=7, start_unix=1_700_000_000)
    assert large[:20] == small


def test_plan_needs_mapped_questions():
    with pytest.raises(ValueError, match="no quiz"):
        simulate_plan([{"quiz_id": 1, "entries": []}])


def test_plan_manifest_reaches_the_training_gate(tmp_path, capsys):
    _, manifest = simulate_plan(_quizzes(), n_learners=20, seed=7, start_unix=1_700_000_000)

    path = cli.save_plan_manifest(manifest, simulated_users=20, data_dir=tmp_path)
    assert path == tmp_path / engine_config.PLAN_MANIFEST_NAME
    # The collector's own marker carries no ceiling; the plan manifest supplies it.
    (tmp_path / engine_config.SIMULATION_MANIFEST_NAME).write_text(json.dumps({"simulated": True}))
    assert _simulator_ceiling_auc(tmp_path) == manifest["ceiling_auc"]

    # A smaller plan than the learners in Moodle must not replace it.
    _, smaller = simulate_plan(_quizzes(), n_learners=5, seed=1, start_unix=1_700_000_000)
    assert cli.save_plan_manifest(smaller, simulated_users=20, data_dir=tmp_path) is None
    assert _simulator_ceiling_auc(tmp_path) == manifest["ceiling_auc"]


def test_dry_run_does_not_touch_moodle(monkeypatch, tmp_path, capsys):
    calls = []

    shortnames = []

    def fake_run_php(script, *args):
        calls.append(script)
        shortnames.append(args[0])
        return {"course_id": 9, "startdate": 1_700_000_000, "quizzes": _quizzes()}

    monkeypatch.setattr(cli.docker, "run_php", fake_run_php)
    # A freshly built course's workspace has no data/ folder yet.
    plan_file = tmp_path / "new_workspace" / "data" / "plan.json"
    cli.main(["simulate-learners", "--learners", "5", "--dry-run", "--plan-out", str(plan_file),
              "--data-dir", str(tmp_path)])

    assert calls == ["export_structure.php"]
    assert shortnames == ["ENG-MICRO-KC"]
    saved = json.loads(plan_file.read_text())
    assert len(saved["learners"]) == 5 and saved["manifest"]["simulated"] is True
    assert not (tmp_path / engine_config.PLAN_MANIFEST_NAME).exists()


# --- lessons -------------------------------------------------------------------


def _lesson_block(kc_id, try_it="- Q one => A one\n- Q two => A two"):
    return (
        f"## {kc_id}\nvideo:\n### Explanation\nSome text.\n### Examples\n- An example\n"
        f"### Common mistake\n- A mistake\n### Try it\n{try_it}\n"
    )


def _lesson_problems(course_dir):
    return content.validate(
        content.load_catalog(course_dir),
        content.load_questions(course_dir),
        content.load_lessons(course_dir),
    )


def test_every_kc_has_one_rendered_lesson():
    spec = content.build_spec()
    assert sorted(lesson["kc_id"] for lesson in spec["lessons"]) == sorted(
        kc["kc_id"] for kc in spec["kcs"]
    )
    html = spec["lessons"][0]["html"]
    for heading in content.LESSON_SECTIONS:
        assert f"<h3>{heading}</h3>" in html
    assert "<details><summary>Show answer</summary>" in html
    assert spec["pass_fraction"] == content.PASS_FRACTION


def test_lesson_validation_catches_missing_duplicate_and_incomplete_lessons(course_copy):
    lessons = course_copy / "lessons"
    unit1 = lessons / "unit1.md"
    text = unit1.read_text(encoding="utf-8")
    # Drop KC002's lesson, repeat KC001's, and add one for an unknown KC.
    start, end = text.index("## KC002"), text.index("## KC003")
    unit1.write_text(text[:start] + text[end:], encoding="utf-8")
    (lessons / "unit9.md").write_text(
        _lesson_block("KC001") + _lesson_block("KC999") + "## KC003b\nvideo:\n### Explanation\nx\n",
        encoding="utf-8",
    )
    problems = _lesson_problems(course_copy)
    assert "KC002: no lesson" in problems
    assert any("second lesson for KC001" in p for p in problems)
    assert any("unknown kc_id 'KC999'" in p for p in problems)
    assert any("unknown kc_id 'KC003b'" in p for p in problems)


def test_lesson_needs_sections_answers_and_no_quiz_reuse(course_copy):
    lessons = course_copy / "lessons"
    for path in lessons.glob("unit*.md"):
        text = path.read_text(encoding="utf-8")
        if "## KC001" in text:
            start = text.index("## KC001")
            end = text.index("\n## ", start) + 1
            stem = 'Choose the correct form: "My sister ___ to work by bus every day."'
            text = text[:start] + _lesson_block("KC001", f"- {stem} => goes\n- No answer here") + text[end:]
            path.write_text(text, encoding="utf-8")
    problems = _lesson_problems(course_copy)
    assert any("KC001 Try it items must read 'question => answer'" in p for p in problems)
    assert any("Try it item repeats a quiz question" in p for p in problems)


def test_lesson_html_is_escaped_and_marks_wrong_forms():
    lesson = {
        "kc_id": "KC001",
        "video": "https://example.org/clip.mp4",
        "sections": {
            "Explanation": ["Use <b> **carefully**.", "", "Second paragraph."],
            "Examples": ["- ~~He go~~ → He **goes**"],
            "Common mistake": ["- A & B"],
            "Try it": ["- 1 < 2? => yes", "- Q => A"],
        },
    }
    html = content.render_lesson(lesson, {"learning_objective": "Goal", "dimension": "Grammar"})
    assert "<b>" not in html and "&lt;b&gt;" in html
    assert "<strong>carefully</strong>" in html and "<del>He go</del>" in html
    assert html.count("<p>") >= 3  # goal + two explanation paragraphs (+ footer)
    assert '<video controls width="100%" src="https://example.org/clip.mp4">' in html
    assert "A &amp; B" in html


# --- page behaviour in the plan ------------------------------------------------


def _course_structure():
    """The mixed-quiz course (layout dimension) as export_structure.php reports
    it after a rebuild."""
    spec = content.build_spec(layout="dimension")
    quizzes, lessons, overviews, cmid = [], {}, {}, 1000
    by_unit_dim = {}
    for entry_id, question in enumerate(spec["questions"], start=1):
        kc = next(k for k in spec["kcs"] if k["kc_id"] == question["kc_id"])
        by_unit_dim.setdefault((kc["unit"], content.DIMENSION_ORDER.index(kc["dimension"])), []).append(
            {"entry_id": entry_id, "kc_id": question["kc_id"]}
        )
    for unit in spec["units"]:
        cmid += 1
        overviews[str(unit["number"])] = cmid
        for dim in range(len(content.DIMENSION_ORDER)):
            for kc in spec["kcs"]:
                if kc["unit"] == unit["number"] and kc["dimension"] == content.DIMENSION_ORDER[dim]:
                    cmid += 1
                    lessons[kc["kc_id"]] = cmid
            cmid += 1
            quizzes.append(
                {
                    "quiz_id": 200 + len(quizzes),
                    "cmid": cmid,
                    "section": unit["number"],
                    "grade": 10.0,
                    "gradepass": 7.0,
                    "entries": by_unit_dim[(unit["number"], dim)],
                }
            )
    return quizzes, lessons, overviews


def _page_plan(n_learners=80, seed=5, **kwargs):
    quizzes, lessons, overviews = _course_structure()
    plan, manifest = simulate_plan(
        quizzes, n_learners=n_learners, seed=seed, start_unix=1_700_000_000,
        lessons=lessons, overviews=overviews, **kwargs,
    )
    return plan, manifest, quizzes, lessons, overviews


def test_page_views_happen_before_attempts_and_use_course_pages():
    plan, manifest, quizzes, lessons, overviews = _page_plan()
    lesson_cm = {cmid: kc for kc, cmid in lessons.items()}
    quiz_kcs = {q["quiz_id"]: {e["kc_id"] for e in q["entries"]} for q in quizzes}
    section_of = {q["quiz_id"]: str(q["section"]) for q in quizzes}
    pages = manifest["page_behaviour"]
    assert pages["lesson_views"] > 0 and pages["overview_views"] > 0
    assert pages["revisions"] > 0 and pages["reviews"] > 0
    previous_end = 0
    for learner in plan:
        previous_end = learner["enrol_start"]
        for attempt in learner["attempts"]:
            times = [view["time"] for view in attempt["views_before"]]
            assert times == sorted(times)
            assert all(previous_end < t < attempt["start"] for t in times)
            for view in attempt["views_before"]:
                if view["kind"] == "lesson":
                    assert lesson_cm[view["cmid"]] == view["kc_id"]
                    assert view["kc_id"] in quiz_kcs[attempt["quiz_id"]]
                else:
                    assert view["cmid"] == overviews[section_of[attempt["quiz_id"]]]
            end = attempt["review_at"] or attempt["finish"]
            assert end >= attempt["finish"]
            previous_end = end


def test_revision_retries_the_failed_quiz_after_reading_its_weak_kcs():
    plan, _, quizzes, _, _ = _page_plan()
    revisions = 0
    for learner in plan:
        for before, attempt in zip(learner["attempts"], learner["attempts"][1:]):
            if not attempt["revision"]:
                continue
            revisions += 1
            assert attempt["quiz_id"] == before["quiz_id"]
            assert sum(a["correct"] for a in before["answers"]) / len(before["answers"]) < 0.7
            per_kc = {}
            for answer in before["answers"]:
                per_kc.setdefault(answer["kc_id"], []).append(answer["correct"])
            weak = {kc for kc, values in per_kc.items() if sum(values) / len(values) < 0.7}
            assert {v["kc_id"] for v in attempt["views_before"] if v["kind"] == "lesson"} == weak
    assert revisions > 0


def test_plans_without_lessons_have_no_page_behaviour():
    quizzes, _, _ = _course_structure()
    plan, manifest = simulate_plan(quizzes, n_learners=30, seed=5, start_unix=1_700_000_000)
    assert manifest["page_behaviour"] is None
    assert all(not a["views_before"] and a["review_at"] is None for l in plan for a in l["attempts"])


def test_page_plans_still_extend_a_smaller_cohort():
    small, *_ = _page_plan(n_learners=15)
    large, *_ = _page_plan(n_learners=40)
    assert large[:15] == small


def test_lesson_gain_raises_accuracy_in_the_generator():
    # A generator property, not evidence about real learners.
    _, without, *_ = _page_plan(n_learners=150, lesson_gain=0.0)
    _, with_gain, *_ = _page_plan(n_learners=150, lesson_gain=2.0)
    assert with_gain["overall_accuracy"] > without["overall_accuracy"]


# --- layout kc: difficulty, CEFR, practice pool, quiz roles ----------------------


def _kc_problems(course_dir):
    return content.validate(
        content.load_catalog(course_dir),
        content.load_questions(course_dir),
        layout="kc",
        pool=content.load_pool(course_dir),
    )


def _append_pool(course_dir, line, unit="unit1.txt"):
    with (course_dir / "questions" / "pool" / unit).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def test_old_question_format_without_difficulty_is_rejected(course_copy):
    _append(course_copy, "KC001 | Stem without a difficulty? | a | b | c | d")
    with pytest.raises(ValueError, match="kc_id \\| difficulty \\| stem"):
        content.load_questions(course_copy)


def test_difficulty_must_be_one_to_three(course_copy):
    _append(course_copy, "KC001 | 4 | A stem with a bad difficulty? | a | b | c | d")
    assert any("difficulty must be one of [1, 2, 3]" in p for p in _problems(course_copy))


def test_cefr_level_must_be_known(course_copy):
    path = course_copy / "kc_catalog.csv"
    with path.open(newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    rows[0]["cefr_level"] = "C3"
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=content.CATALOG_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    assert any("KC001: CEFR level must be one of" in p for p in _problems(course_copy))


def test_shipped_pool_is_valid_and_spans_all_difficulties():
    catalog = content.load_catalog()
    pool = content.load_pool()
    assert not _kc_problems(content.COURSE_DIR)
    levels = {}
    for question in pool:
        levels.setdefault(question["kc_id"], []).append(int(question["difficulty"]))
    assert set(levels) == {kc["kc_id"] for kc in catalog}
    for found in levels.values():
        assert len(found) >= content.POOL_MIN_PER_KC
        assert set(found) == set(content.DIFFICULTY_LEVELS)
        assert found.count(3) >= content.CHALLENGE_SLOTS


def test_pool_must_not_repeat_a_check_question(course_copy):
    line = 'KC001 | 1 | Choose the correct form: "My sister ___ to work by bus every day." | goes | go | going | gone'
    _append_pool(course_copy, line)
    assert any("repeats the check question at unit1.txt" in p for p in _kc_problems(course_copy))


def test_pool_needs_every_difficulty_and_enough_hard_items(course_copy):
    path = course_copy / "questions" / "pool" / "unit1.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.startswith("KC001 | 3 |")]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    problems = _kc_problems(course_copy)
    assert "KC001: 2 pool question(s), need at least 4" in problems
    assert "KC001: pool has no question of difficulty [3]" in problems
    assert "KC001: 0 hard pool question(s), a challenge quiz needs 2" in problems


def test_dimension_layout_ignores_the_pool(course_copy):
    shutil.rmtree(course_copy / "questions" / "pool")
    assert content.build_spec(course_copy, layout="dimension")["layout"] == "dimension"
    with pytest.raises(ValueError, match="0 pool question"):
        content.build_spec(course_copy, layout="kc")


def test_kc_layout_quizzes_carry_a_role_in_their_id_number():
    spec = content.build_spec()
    quizzes = spec["quizzes"]
    kc_ids = [kc["kc_id"] for kc in spec["kcs"]]
    by_role = {}
    for quiz in quizzes:
        by_role.setdefault(quiz["role"], []).append(quiz)
        prefix, _, suffix = quiz["idnumber"].partition("-")
        assert content.QUIZ_ROLES[prefix] == quiz["role"] and suffix
    assert len({quiz["idnumber"] for quiz in quizzes}) == len(quizzes)
    assert {role: len(items) for role, items in by_role.items()} == {
        "checkin": 8, "practice": 96, "check": 96, "review": 7, "challenge": 96,
    }
    questions = {q["name"]: q for q in spec["questions"]}
    for check in by_role["check"]:
        # Fixed check questions of its one KC, with the pass mark.
        assert check["slots"] == [] and check["pass_fraction"] == content.PASS_FRACTION
        assert {questions[name]["kc_id"] for name in check["questions"]} == set(check["kc_ids"])
        assert all(questions[name]["item_set"] == content.CHECK_SET for name in check["questions"])
        assert content.MIN_QUESTIONS_PER_KC <= len(check["questions"]) <= content.MAX_QUESTIONS_PER_KC
    for role in ("checkin", "practice", "review", "challenge"):
        for quiz in by_role[role]:
            # Random pool questions only, never a check question, and no pass mark.
            assert quiz["questions"] == [] and quiz["pass_fraction"] is None
            assert [slot["kc_id"] for slot in quiz["slots"]] and all(
                content.TAG_POOL in slot["tags"] for slot in quiz["slots"]
            )
            assert {slot["kc_id"] for slot in quiz["slots"]} == set(quiz["kc_ids"])
    assert all(len(q["slots"]) == content.PRACTICE_SLOTS for q in by_role["practice"])
    assert all(
        len(q["slots"]) == content.CHALLENGE_SLOTS
        and all(content.difficulty_tag(3) in slot["tags"] for slot in q["slots"])
        for q in by_role["challenge"]
    )
    assert [q["max_attempts"] for q in by_role["checkin"]] == [1] * 8
    assert {q["max_attempts"] for role in ("practice", "check", "review", "challenge") for q in by_role[role]} == {0}
    assert sorted(kc for q in by_role["practice"] for kc in q["kc_ids"]) == sorted(kc_ids)


def test_check_in_asks_every_kc_of_its_unit_before_teaching():
    spec = content.build_spec()
    unit_of = {kc["kc_id"]: kc["unit"] for kc in spec["kcs"]}
    quizzes = {quiz["idnumber"]: quiz for quiz in spec["quizzes"]}
    for section in spec["sections"][:-1]:
        items = section["items"]
        assert items[0] == {"type": "overview"}
        checkin = quizzes[items[1]["idnumber"]]
        assert checkin["role"] == "checkin"
        assert sorted(checkin["kc_ids"]) == sorted(k for k, u in unit_of.items() if u == section["number"])
        # Then lesson -> practice -> check for each KC, in that order.
        body = items[2:-1] if section["number"] > 1 else items[2:]
        for lesson, practice, check in zip(body[0::3], body[1::3], body[2::3]):
            kc_id = lesson["kc_id"]
            assert quizzes[practice["idnumber"]]["role"] == "practice"
            assert quizzes[practice["idnumber"]]["kc_ids"] == [kc_id]
            assert quizzes[check["idnumber"]]["idnumber"] == content.quiz_idnumber("check", kc_id)
    challenge = spec["sections"][-1]
    assert challenge["name"] == content.CHALLENGE_SECTION_NAME
    assert {quizzes[item["idnumber"]]["role"] for item in challenge["items"]} == {"challenge"}


def test_review_quizzes_ask_two_earlier_kcs_per_dimension():
    spec = content.build_spec()
    kcs = {kc["kc_id"]: kc for kc in spec["kcs"]}
    reviews = [quiz for quiz in spec["quizzes"] if quiz["role"] == "review"]
    assert [quiz["unit"] for quiz in reviews] == list(range(2, 9))
    seen = set()
    for review in reviews:
        assert len(review["kc_ids"]) == len(set(review["kc_ids"]))
        assert all(kcs[kc_id]["unit"] < review["unit"] for kc_id in review["kc_ids"])
        assert any(kcs[kc_id]["unit"] == review["unit"] - 1 for kc_id in review["kc_ids"])
        per_dimension = {}
        for kc_id in review["kc_ids"]:
            per_dimension[kcs[kc_id]["dimension"]] = per_dimension.get(kcs[kc_id]["dimension"], 0) + 1
        assert per_dimension == dict.fromkeys(content.DIMENSION_ORDER, content.REVIEW_KCS_PER_DIMENSION)
        seen.update(review["kc_ids"])
    # Deterministic: a rebuild asks about the same KCs.
    assert reviews == [q for q in content.build_spec()["quizzes"] if q["role"] == "review"]
    assert len(seen) > 30  # spread over many KCs, not the same few


def test_kc_layout_questions_carry_difficulty_set_and_cefr_tags():
    spec = content.build_spec()
    cefr = {kc["kc_id"]: kc["cefr"] for kc in spec["kcs"]}
    names = [q["name"] for q in spec["questions"]]
    assert len(names) == len(set(names))
    for question in spec["questions"]:
        expected_set = content.TAG_CHECK if question["item_set"] == content.CHECK_SET else content.TAG_POOL
        assert question["tags"] == [
            expected_set,
            content.difficulty_tag(question["difficulty"]),
            content.cefr_tag(cefr[question["kc_id"]]),
        ]
        assert all(tag == tag.lower() for tag in question["tags"])
        letter = "Q" if question["item_set"] == content.CHECK_SET else "P"
        assert question["name"].startswith(f"{question['kc_id']} {letter}")
    assert set(cefr.values()) <= set(content.CEFR_LEVELS)


def test_kc_lesson_points_to_its_own_practice_and_check():
    spec = content.build_spec()
    dim_spec = content.build_spec(layout="dimension")
    assert "practice quiz on this point" in spec["lessons"][0]["html"]
    assert "unit's" not in spec["lessons"][0]["html"]
    assert "practise this point in the unit's" in dim_spec["lessons"][0]["html"]


def test_build_refuses_quiz_roles_until_the_php_supports_them(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.docker, "copy_in", lambda *a: calls.append(a))
    monkeypatch.setattr(cli.docker, "run_php", lambda *a: calls.append(a))
    monkeypatch.setattr(cli, "BUILD_SUPPORTS_QUIZ_ROLES", False)
    with pytest.raises(SystemExit, match="cannot build them yet"):
        cli.main(["build-course", "--layout", "kc"])
    assert calls == []  # nothing copied into or run in Moodle
