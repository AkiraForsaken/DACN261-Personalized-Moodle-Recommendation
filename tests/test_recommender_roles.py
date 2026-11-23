"""Quiz roles (layout kc): practice, check-in, review and challenge quizzes,
the retry cap and the challenge rule.

Course: Unit 1 = overview, check-in (KC-A, KC-B), then lesson -> practice ->
check for KC-A and KC-B; Unit 2 = check-in (KC-C), lesson/practice/check for
KC-C, review (KC-A, KC-B); Section 3 = one challenge quiz per KC. KC-A and
KC-C are grammar (KC-A is KC-C's assumed prerequisite), KC-B is reading.
"""

import json

import pandas as pd

from recommender import config, schemas
from recommender.course import build_course_graph
from recommender.generate import recommend, recommend_all, summarize
from recommender.inputs import ActivityState, KCState, LearnerState, RecommenderInputs
from recommender.reasons import reason_text
from recommender.rules import (
    challenge_candidates,
    contingency_candidates,
    failed_streak,
    forward_candidates,
    remediation_candidates,
    stuck_quizzes,
)

(
    OVERVIEW, CHECKIN_1, LESSON_A, PRACTICE_A, CHECK_A, LESSON_B, PRACTICE_B, CHECK_B,
    CHECKIN_2, LESSON_C, PRACTICE_C, CHECK_C, REVIEW_2,
    CHALLENGE_A, CHALLENGE_B, CHALLENGE_C,
) = range(201, 217)

ACTIVITIES = [
    # module, section, type, name, role, pass fraction
    (OVERVIEW, 1, "page", "Unit 1 overview", "", None),
    (CHECKIN_1, 1, "quiz", "Unit 1 check-in", "checkin", None),
    (LESSON_A, 1, "page", "Lesson: A", "", None),
    (PRACTICE_A, 1, "quiz", "Practice: A", "practice", None),
    (CHECK_A, 1, "quiz", "Check: A", "check", 0.7),
    (LESSON_B, 1, "page", "Lesson: B", "", None),
    (PRACTICE_B, 1, "quiz", "Practice: B", "practice", None),
    (CHECK_B, 1, "quiz", "Check: B", "check", 0.7),
    (CHECKIN_2, 2, "quiz", "Unit 2 check-in", "checkin", None),
    (LESSON_C, 2, "page", "Lesson: C", "", None),
    (PRACTICE_C, 2, "quiz", "Practice: C", "practice", None),
    (CHECK_C, 2, "quiz", "Check: C", "check", 0.7),
    (REVIEW_2, 2, "quiz", "Unit 2 review", "review", None),
    (CHALLENGE_A, 3, "quiz", "Challenge: A", "challenge", None),
    (CHALLENGE_B, 3, "quiz", "Challenge: B", "challenge", None),
    (CHALLENGE_C, 3, "quiz", "Challenge: C", "challenge", None),
]
LINKS = [
    ("KC-A", LESSON_A, "learn", None), ("KC-B", LESSON_B, "learn", None),
    ("KC-C", LESSON_C, "learn", None),
    ("KC-A", CHECKIN_1, "practice", 1), ("KC-B", CHECKIN_1, "practice", 1),
    ("KC-A", PRACTICE_A, "practice", 2), ("KC-A", CHECK_A, "practice", 4),
    ("KC-B", PRACTICE_B, "practice", 2), ("KC-B", CHECK_B, "practice", 4),
    ("KC-C", CHECKIN_2, "practice", 1),
    ("KC-C", PRACTICE_C, "practice", 2), ("KC-C", CHECK_C, "practice", 4),
    ("KC-A", REVIEW_2, "practice", 1), ("KC-B", REVIEW_2, "practice", 1),
    ("KC-A", CHALLENGE_A, "practice", 2), ("KC-B", CHALLENGE_B, "practice", 2),
    ("KC-C", CHALLENGE_C, "practice", 2),
]
KC_NAMES = {"KC-A": "Topic A", "KC-B": "Topic B", "KC-C": "Topic C"}
DIMENSIONS = {"KC-A": "grammar", "KC-B": "reading", "KC-C": "grammar"}


def _course():
    catalog = pd.DataFrame(
        [
            {
                "course_module_id": module, "section_number": section, "activity_type": kind,
                "activity_name": name, "topic": f"Unit {section}", "quiz_role": role,
                "pass_fraction": pass_fraction, "completion_tracking": 2, "visible": 1,
                "sequence": index, "activity_id": module,
            }
            for index, (module, section, kind, name, role, pass_fraction) in enumerate(ACTIVITIES, 1)
        ]
    )
    links = pd.DataFrame(LINKS, columns=["kc_id", "course_module_id", "role", "question_count"])
    return build_course_graph(catalog, links, KC_NAMES, DIMENSIONS)


def _learner(activities=None, kcs=None):
    return LearnerState("7", "simulated", 1, activities or {}, kcs or {})


def _inputs(*learners):
    return RecommenderInputs(
        course=_course(), learners={l.learner_id: l for l in learners}, low_accuracy=0.6,
        as_of=None, mapping_version="map-1", input_files={},
    )


def _failed(*times, last_viewed=None):
    """A check quiz failed at each of ``times`` (Unix seconds)."""
    return ActivityState(
        "failed", last_viewed=last_viewed, last_attempt=times[-1], attempt_count=len(times),
        best_score=0.25, pass_fraction=0.7, attempts=tuple((t, False) for t in times),
    )


def _passed(time):
    return ActivityState(
        "passed", last_attempt=time, attempt_count=1, best_score=1.0, pass_fraction=0.7,
        attempts=((time, True),),
    )


def _codes(candidates):
    return [(c.module_id, c.reason_code) for c in candidates]


# ------------------------------------------------------------------ the course graph
def test_a_kc_quiz_is_its_check_quiz_and_other_roles_are_kept_apart():
    course = _course()
    assert course.quiz_of_kc == {"KC-A": CHECK_A, "KC-B": CHECK_B, "KC-C": CHECK_C}
    assert course.practice_of_kc == {"KC-A": PRACTICE_A, "KC-B": PRACTICE_B, "KC-C": PRACTICE_C}
    assert course.challenge_of_kc == {"KC-A": CHALLENGE_A, "KC-B": CHALLENGE_B, "KC-C": CHALLENGE_C}
    assert course.checkin_of_section == {1: CHECKIN_1, 2: CHECKIN_2}
    assert course.check_quizzes() == [CHECK_A, CHECK_B, CHECK_C]
    assert course.kcs_of_quiz[CHECKIN_1] == ["KC-A", "KC-B"]
    assert course.quiz_role(LESSON_A) is None and course.quiz_role(REVIEW_2) == "review"


# ------------------------------------------------------------------ forward items
def test_a_new_learner_meets_the_check_in_before_the_lessons_and_no_challenge():
    codes = _codes(forward_candidates(_learner(), _course(), config.MAX_PREPARE))
    modules = [module for module, _ in codes]
    assert modules[:5] == [OVERVIEW, CHECKIN_1, LESSON_A, PRACTICE_A, CHECK_A]
    assert codes[0][1] == schemas.START_COURSE
    assert not {CHALLENGE_A, CHALLENGE_B, CHALLENGE_C} & set(modules)


def test_check_in_and_practice_drop_out_once_the_unit_and_kc_are_checked():
    learner = _learner(activities={CHECK_A: _passed(100)})
    modules = [c.module_id for c in forward_candidates(learner, _course(), config.MAX_PREPARE)]
    # Unit 1's check-in diagnoses before teaching: no longer useful.
    assert CHECKIN_1 not in modules
    # KC-A is checked: its lesson and practice are not "next" any more.
    assert LESSON_A not in modules and PRACTICE_A not in modules
    # Unit 2's check-in is still ahead.
    assert CHECKIN_2 in modules and PRACTICE_B in modules


def test_prediction_prepares_for_the_next_check_quiz_not_a_practice_quiz():
    learner = _learner(
        activities={OVERVIEW: ActivityState("completed", last_viewed=10)},
        kcs={"KC-A": KCState(0, None, 0.4, None, "prediction_based_concern")},
    )
    candidates = forward_candidates(learner, _course(), config.MAX_PREPARE)
    prepare = [c for c in candidates if c.reason_code == schemas.PREPARE_PREDICTED_DIFFICULTY]
    assert [(c.module_id, c.evidence["upcoming_quiz_module_id"]) for c in prepare] == [(LESSON_A, CHECK_A)]


# ------------------------------------------------------------------ practice target
def test_a_weak_kc_whose_lesson_was_reread_goes_to_its_practice_quiz():
    learner = _learner(
        activities={
            CHECK_A: _passed(100),
            LESSON_A: ActivityState("completed", last_viewed=200, view_times=(200,)),
        },
        kcs={"KC-A": KCState(5, 0.4, 0.5, 150, "evidence_based_concern")},
    )
    codes = _codes(remediation_candidates(learner, _course(), 0.6))
    assert codes == [(PRACTICE_A, schemas.PRACTICE_WEAK_KC)]


# ------------------------------------------------------------------ retry cap
WEAK_C = {"KC-C": KCState(12, 0.25, 0.3, 300, "evidence_based_concern")}


def test_below_the_cap_a_failed_check_is_revised_and_retried():
    learner = _learner(activities={CHECK_C: _failed(100, 200)}, kcs=WEAK_C)
    assert failed_streak(learner, CHECK_C) == 2 and stuck_quizzes(learner, _course()) == {}
    codes = _codes(remediation_candidates(learner, _course(), 0.6))
    assert codes == [(LESSON_C, schemas.REVISE_BEFORE_RETRY), (CHECK_C, schemas.RETRY_QUIZ)]
    # Failing once more reaches the cap, so its precomputed branch has no retry.
    branch = [(c.module_id, c.reason_code) for c in contingency_candidates(learner, _course(), 0.6)
              if c.trigger_module_id == CHECK_C]
    assert (CHECK_C, schemas.RETRY_QUIZ) not in branch and branch[0] == (LESSON_C, schemas.REVISE_BEFORE_RETRY)


def test_at_the_cap_the_learner_is_sent_back_once_then_moves_on():
    learner = _learner(activities={CHECK_C: _failed(100, 200, 300)}, kcs=WEAK_C)
    course = _course()
    assert stuck_quizzes(learner, course) == {CHECK_C: 3}

    remediation = remediation_candidates(learner, course, 0.6)
    codes = _codes(remediation)
    # No revise-before-retry and no retry of the stuck quiz.
    assert (CHECK_C, schemas.RETRY_QUIZ) not in codes
    assert (LESSON_C, schemas.REVISE_BEFORE_RETRY) not in codes
    # The earlier-unit prerequisite (KC-A, not assessed), marked as the cap.
    prerequisite = next(c for c in remediation if c.reason_code == schemas.REVISIT_PREREQUISITE)
    assert prerequisite.module_id == LESSON_A
    assert prerequisite.evidence["retry_cap_reached"] and prerequisite.evidence["quiz_failed_in_a_row"] == 3
    assert "in 3 tries" in reason_text(prerequisite, course)
    # KC-C itself goes to low-stakes review/practice, not the check quiz.
    assert (LESSON_C, schemas.REVIEW_WEAK_KC) in codes
    # No failure branch for a stuck quiz.
    assert not [c for c in contingency_candidates(learner, course, 0.6) if c.trigger_module_id == CHECK_C]
    # The way forward says why, for the learner and the teacher, on the first
    # "continue" item that is really in the list.
    shown = recommend(learner, _inputs(learner), top_n=5)
    first = next(c for c in shown if c.reason_code == schemas.CONTINUE_COURSE)
    assert first.evidence["stuck_quiz_module_id"] == CHECK_C
    assert first.evidence["stuck_failed_attempts"] == 3
    assert reason_text(first, course).startswith("Move on for now; you can come back to “Check: C” later.")


def test_the_prerequisite_is_offered_once_then_only_practice_remains():
    learner = _learner(
        activities={
            CHECK_C: _failed(100, 200, 300),
            LESSON_A: ActivityState("completed", last_viewed=400, view_times=(400,)),
            LESSON_C: ActivityState("completed", last_viewed=410, view_times=(410,)),
        },
        kcs=WEAK_C,
    )
    codes = _codes(remediation_candidates(learner, _course(), 0.6))
    assert codes == [(PRACTICE_C, schemas.PRACTICE_WEAK_KC)]


def test_passing_another_check_releases_the_stuck_quiz_for_one_more_retry():
    released = _learner(activities={CHECK_C: _failed(100, 200, 300), CHECK_A: _passed(350)}, kcs=WEAK_C)
    assert stuck_quizzes(released, _course()) == {}
    assert (CHECK_C, schemas.RETRY_QUIZ) in _codes(remediation_candidates(released, _course(), 0.6))
    # Failing it again after the release makes it stuck again.
    again = _learner(
        activities={CHECK_C: _failed(100, 200, 300, 400), CHECK_A: _passed(350)}, kcs=WEAK_C
    )
    assert stuck_quizzes(again, _course()) == {CHECK_C: 4}


def test_without_an_attempt_history_no_cap_applies():
    learner = _learner(
        activities={CHECK_C: ActivityState("failed", last_attempt=300, attempt_count=5,
                                           best_score=0.2, pass_fraction=0.7)},
        kcs=WEAK_C,
    )
    assert stuck_quizzes(learner, _course()) == {}


# ------------------------------------------------------------------ challenge
STRONG_A = {"KC-A": KCState(6, 5 / 6, 0.8, 120, "no_current_concern")}


def test_a_strong_checked_kc_gets_its_challenge_quiz_second_in_the_forward_items():
    learner = _learner(activities={CHECK_A: _passed(120)}, kcs=STRONG_A)
    forward = forward_candidates(learner, _course(), config.MAX_PREPARE)
    assert forward[1].module_id == CHALLENGE_A
    assert forward[1].reason_code == schemas.CHALLENGE_STRONG_KC
    assert forward[1].evidence["check_quiz_module_id"] == CHECK_A
    assert schemas.REASON_GROUPS[schemas.CHALLENGE_STRONG_KC] == schemas.GROUP_FORWARD
    assert "harder questions" in reason_text(forward[1], _course())
    # In the visible list too (forward slots).
    top = recommend(learner, _inputs(learner), top_n=5)
    assert CHALLENGE_A in [c.module_id for c in top]


def test_no_challenge_without_enough_evidence_a_pass_or_once_attempted():
    course = _course()
    few = _learner(activities={CHECK_A: _passed(120)},
                   kcs={"KC-A": KCState(4, 1.0, 0.9, 120, "no_current_concern")})
    weak = _learner(activities={CHECK_A: _passed(120)},
                    kcs={"KC-A": KCState(10, 0.7, 0.7, 120, "no_current_concern")})
    unchecked = _learner(kcs=STRONG_A)
    tried = _learner(
        activities={CHECK_A: _passed(120),
                    CHALLENGE_A: ActivityState("completed", last_attempt=130, attempt_count=1)},
        kcs=STRONG_A,
    )
    for learner in (few, weak, unchecked, tried):
        assert challenge_candidates(learner, course) == []


def test_at_most_one_challenge_the_most_recently_checked_kc():
    learner = _learner(
        activities={CHECK_A: _passed(120), CHECK_B: _passed(220)},
        kcs={**STRONG_A, "KC-B": KCState(5, 1.0, 0.9, 220, "no_current_concern")},
    )
    assert _codes(challenge_candidates(learner, _course())) == [(CHALLENGE_B, schemas.CHALLENGE_STRONG_KC)]


# ------------------------------------------------------------------ contract
def test_generated_rows_with_cap_and_challenge_meet_the_contract():
    stuck = _learner(activities={CHECK_C: _failed(100, 200, 300)}, kcs=WEAK_C)
    stuck.learner_id = "8"
    strong = _learner(activities={CHECK_A: _passed(120)}, kcs=STRONG_A)
    inputs = _inputs(stuck, strong)
    frame = recommend_all(inputs, full_queue=True)

    challenge = frame[frame["reason_code"].eq(schemas.CHALLENGE_STRONG_KC)]
    assert set(challenge["group"]) == {"forward"} and set(challenge["basis"]) == {"evidence"}
    stuck_rows = frame[frame["learner_id"].eq("8")]
    assert not ((stuck_rows["course_module_id"] == CHECK_C) & stuck_rows["trigger_cmid"].isna()).any()
    assert any("stuck_quiz_module_id" in json.loads(e) for e in stuck_rows["evidence"])
    assert summarize(frame, inputs)["learners_at_retry_cap"] == 1
