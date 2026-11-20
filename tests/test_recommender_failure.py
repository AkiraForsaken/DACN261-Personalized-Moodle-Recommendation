"""What happens when a learner fails a quiz: contingency branches and escalation.

Course of tests/test_recommender.py: Unit 1 = lessons A, B, quiz 1 (KC-A, KC-B);
Unit 2 = lesson C, quiz 2 (KC-C); Unit 3 = lesson D, quiz 3 (KC-D). Here KC-A,
KC-C and KC-D are grammar, KC-B is reading, so KC-A is the assumed prerequisite
of KC-C, and KC-C (then KC-A) of KC-D.
"""

import pandas as pd
import pytest

from recommender import config, schemas
from recommender.generate import recommend, recommend_all
from recommender.inputs import ActivityState, KCState, RecommenderInputs, _attach_history
from recommender.ranking import effective_queue, window
from recommender.rules import contingency_candidates
from tests.test_recommender import (
    LESSON_A,
    LESSON_B,
    LESSON_C,
    LESSON_D,
    OVERVIEW,
    QUIZ_1,
    QUIZ_2,
    QUIZ_3,
    _codes,
    _course,
    _learner,
)

DIMENSIONS = {"KC-A": "grammar", "KC-B": "reading", "KC-C": "grammar", "KC-D": "grammar"}


def _graph():
    course = _course()
    course.kc_dimension = dict(DIMENSIONS)
    return course


def _inputs(*learners):
    return RecommenderInputs(
        course=_graph(),
        learners={learner.learner_id: learner for learner in learners},
        low_accuracy=0.6,
        as_of=None,
        mapping_version="map-1",
        input_files={},
    )


def _branch(candidates, quiz):
    return [(c.module_id, c.reason_code) for c in candidates if c.trigger_module_id == quiz]


# ------------------------------------------------------------- the course graph
def test_prerequisites_are_same_dimension_earlier_units_nearest_first():
    course = _graph()
    assert course.prerequisite_kcs("KC-D") == ["KC-C", "KC-A"]
    assert course.prerequisite_kcs("KC-C") == ["KC-A"]
    assert course.prerequisite_kcs("KC-A") == []  # nothing earlier
    assert course.prerequisite_kcs("KC-B") == []  # no other reading KC


# --------------------------------------------------------- contingency branches
def test_every_quiz_not_yet_passed_gets_a_revise_then_retry_branch():
    learner = _learner()
    rows = contingency_candidates(learner, _graph(), 0.6)

    assert _branch(rows, QUIZ_1) == [
        (LESSON_A, schemas.REVISE_BEFORE_RETRY),
        (LESSON_B, schemas.REVISE_BEFORE_RETRY),
        (QUIZ_1, schemas.RETRY_QUIZ),
    ]
    assert _branch(rows, QUIZ_2) == [(LESSON_C, schemas.REVISE_BEFORE_RETRY), (QUIZ_2, schemas.RETRY_QUIZ)]
    assert all(c.evidence["contingent"] for c in rows)


def test_a_passed_quiz_has_no_branch_and_lessons_are_ordered_weakest_first():
    learner = _learner(
        activities={QUIZ_2: ActivityState("passed", last_attempt=10, attempt_count=1)},
        kcs={
            # KC-A answered well; KC-B unanswered, predicted weak.
            "KC-A": KCState(4, 0.75, 0.7, 10, "no_current_concern"),
            "KC-B": KCState(0, None, 0.4, None, "prediction_based_concern"),
        },
    )
    rows = contingency_candidates(learner, _graph(), 0.6)

    assert _branch(rows, QUIZ_1)[:2] == [
        (LESSON_B, schemas.REVISE_BEFORE_RETRY),
        (LESSON_A, schemas.REVISE_BEFORE_RETRY),
    ]
    assert _branch(rows, QUIZ_2) == []


def test_the_branch_of_a_failed_quiz_adds_earlier_unit_lessons():
    learner = _learner(
        activities={
            QUIZ_2: ActivityState("failed", last_attempt=300, attempt_count=1, best_score=0.4,
                                  pass_fraction=0.7),
        },
        kcs={"KC-C": KCState(4, 0.25, 0.4, 300, "evidence_based_concern")},
    )
    rows = contingency_candidates(learner, _graph(), 0.6)

    assert _branch(rows, QUIZ_2) == [
        (LESSON_C, schemas.REVISE_BEFORE_RETRY),
        (LESSON_A, schemas.REVISIT_PREREQUISITE),  # KC-A: grammar, Unit 1, not assessed
        (QUIZ_2, schemas.RETRY_QUIZ),
    ]
    assert all(c.evidence.get("failed_again") for c in rows if c.trigger_module_id == QUIZ_2)


def test_queue_rows_come_first_and_branches_after_with_their_trigger():
    learner = _learner()
    frame = recommend_all(_inputs(learner), full_queue=True)

    branch = frame["trigger_cmid"].notna()
    assert frame.loc[branch, "rank"].min() > frame.loc[~branch, "rank"].max()
    assert frame.loc[branch, "group"].eq(schemas.GROUP_CONTINGENCY).all()
    first = frame[branch].iloc[0]
    assert first["reason_text"].startswith("You did not pass “Unit 1 practice”.")
    # The top-N (what the learner sees before failing anything) is unchanged.
    top = recommend_all(_inputs(learner))
    assert frame.loc[frame["rank"].le(config.TOP_N), "course_module_id"].tolist() == top[
        "course_module_id"
    ].tolist()


def test_a_branch_before_the_queue_is_refused():
    frame = recommend_all(_inputs(_learner()), full_queue=True)
    broken = frame.copy()
    branch = broken["trigger_cmid"].notna()
    broken.loc[branch, "rank"] = range(1, int(branch.sum()) + 1)
    broken.loc[~branch, "rank"] = range(int(branch.sum()) + 1, len(broken) + 1)
    with pytest.raises(ValueError, match="after the queue"):
        schemas.validate_recommendations(broken)


# ------------------------------------------------- the window after a failure
def _before_quiz_1():
    """Overview and both Unit 1 lessons read; quiz 1 is next."""
    return _learner(
        activities={
            OVERVIEW: ActivityState("completed", last_viewed=10),
            LESSON_A: ActivityState("completed", last_viewed=20),
            LESSON_B: ActivityState("completed", last_viewed=30),
        }
    )


def test_failing_the_quiz_turns_the_list_back_to_its_lessons():
    learner = _before_quiz_1()
    queue = recommend(learner, _inputs(learner), full_queue=True)

    before = window(queue, set(), config.TOP_N, config.MAX_REMEDIATION)
    assert before[0].module_id == QUIZ_1

    # Failed: the branch replaces the quiz's plain row and comes first.
    after = window(queue, set(), config.TOP_N, config.MAX_REMEDIATION, active_triggers={QUIZ_1})
    assert _codes(after)[:3] == [
        (LESSON_A, schemas.REVISE_BEFORE_RETRY),
        (LESSON_B, schemas.REVISE_BEFORE_RETRY),
        (QUIZ_1, schemas.RETRY_QUIZ),
    ]
    assert any(c.module_id == LESSON_C for c in after)  # a way forward stays
    # Lessons reread after the fail: the retry is first.
    reread = window(queue, {LESSON_A, LESSON_B}, config.TOP_N, config.MAX_REMEDIATION,
                    active_triggers={QUIZ_1})
    assert _codes(reread)[0] == (QUIZ_1, schemas.RETRY_QUIZ)


def test_branches_of_quizzes_not_failed_stay_hidden():
    learner = _before_quiz_1()
    queue = recommend(learner, _inputs(learner), full_queue=True)
    plain = [c for c in queue if c.trigger_module_id is None]
    assert [c.module_id for c in effective_queue(queue)] == [c.module_id for c in plain]


# ----------------------------------------------------------- escalation (refresh)
def _failed_quiz_2_twice(lesson_c_views):
    return _learner(
        activities={
            LESSON_C: ActivityState("completed", last_viewed=max(lesson_c_views),
                                    view_times=tuple(lesson_c_views)),
            QUIZ_2: ActivityState(
                "failed", last_attempt=300, attempt_count=2, best_score=0.5, pass_fraction=0.7,
                attempts=((100.0, False), (300.0, False)),
            ),
        },
        kcs={"KC-C": KCState(8, 0.375, 0.4, 300, "evidence_based_concern")},
    )


def test_failing_again_after_rereading_goes_back_to_an_earlier_unit():
    learner = _failed_quiz_2_twice([50, 200])  # reread between the two attempts

    chosen = recommend(learner, _inputs(learner))

    assert _codes(chosen)[:2] == [
        (LESSON_C, schemas.REVISE_BEFORE_RETRY),
        (LESSON_A, schemas.REVISIT_PREREQUISITE),
    ]
    prerequisite = chosen[1]
    assert prerequisite.evidence["for_kc_id"] == "KC-C"
    assert prerequisite.evidence["prerequisite_basis"] == "not_assessed"
    assert prerequisite.evidence["relation"] == config.PREREQUISITE_RELATION


def test_failing_again_without_rereading_keeps_the_plain_loop():
    learner = _failed_quiz_2_twice([50])  # read only before the first attempt

    codes = _codes(recommend(learner, _inputs(learner)))

    assert (LESSON_C, schemas.REVISE_BEFORE_RETRY) in codes
    assert schemas.REVISIT_PREREQUISITE not in {code for _, code in codes}


def test_a_strong_earlier_kc_is_not_a_prerequisite_to_revisit():
    learner = _failed_quiz_2_twice([50, 200])
    learner.kcs["KC-A"] = KCState(4, 1.0, 0.9, 10, "no_current_concern")

    codes = _codes(recommend(learner, _inputs(learner)))

    assert schemas.REVISIT_PREREQUISITE not in {code for _, code in codes}


def test_rereading_after_the_second_fail_puts_prerequisites_before_the_retry():
    learner = _failed_quiz_2_twice([50, 200, 400])  # reread again after attempt 2
    learner.kcs["KC-A"] = KCState(4, 0.25, 0.3, 10, "evidence_based_concern")

    codes = _codes(recommend(learner, _inputs(learner)))

    assert codes.index((LESSON_A, schemas.REVISIT_PREREQUISITE)) < codes.index(
        (QUIZ_2, schemas.RETRY_QUIZ)
    )


# --------------------------------------------------------------- history inputs
def test_view_history_respects_the_build_cutoff(tmp_path):
    learner = _learner(activities={LESSON_C: ActivityState("completed", last_viewed=100)})
    pd.DataFrame(
        {
            "learner_id": ["7", "7", "7"],
            "course_module_id": [LESSON_C, LESSON_C, LESSON_C],
            "event_category": ["content_view", "content_view", "quiz_attempt"],
            "timestamp_unix": [100, 5_000_000_000, 150],
        }
    ).to_csv(tmp_path / "learning_events.csv", index=False)
    attempts = pd.DataFrame(
        {
            "learner_id": ["7"], "course_module_id": [LESSON_C],
            "finished_at": ["1970-01-01T00:02:00+00:00"], "passed": [0],
        }
    )

    _attach_history({"7": learner}, attempts, tmp_path, "2000-01-01T00:00:00+00:00")

    state = learner.activities[LESSON_C]
    assert state.view_times == (100.0,)  # the later view is after the cutoff
    assert state.attempts == ((120.0, False),)
