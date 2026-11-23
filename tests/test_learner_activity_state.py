"""learner_activity_state: what each learner did with each activity."""

import json

import pandas as pd

from learner_model.activity_state import ACTIVITY_STATE_COLUMNS, build_activity_state
from learner_model.builder import build_complete_learner_model

T0 = 1_790_000_000


def _activities(pass_fraction=0.7):
    rows = [
        dict(course_module_id=10, activity_type="page", activity_id=1, sequence=1),
        dict(course_module_id=11, activity_type="page", activity_id=2, sequence=2),
        dict(course_module_id=12, activity_type="quiz", activity_id=50, sequence=3),
    ]
    frame = pd.DataFrame(rows).assign(
        activity_name="x", topic="Unit 1", completion_tracking=2, visible=1, section_number=1
    )
    if pass_fraction is not None:
        frame["pass_fraction"] = [None, None, pass_fraction]
    return frame


def _view(learner, module, offset):
    return dict(
        learner_id=learner,
        course_module_id=module,
        event_category="content_view",
        timestamp_unix=T0 + offset,
        is_learning_event=1,
        session_id=f"u{learner}-s1",
        seconds_since_prev_event=10,
    )


def _attempt(learner, offset, score, attempt=1):
    return dict(
        learner_id=learner,
        quiz_id=50,
        attempt=attempt,
        normalized_score=score,
        finish_time_unix=T0 + offset,
        duration_seconds=60,
    )


def _completion(learner, module, state, offset):
    return dict(
        learner_id=learner,
        course_module_id=module,
        completion_state=state,
        time_modified_unix=T0 + offset,
    )


def _state(events=(), attempts=(), completions=(), activities=None):
    frame, disagreements = build_activity_state(
        pd.DataFrame(list(events)),
        pd.DataFrame(list(attempts)),
        pd.DataFrame(list(completions)),
        _activities() if activities is None else activities,
    )
    return frame.set_index(["learner_id", "course_module_id"]), disagreements


def test_one_row_per_touched_activity_with_views_attempts_and_completion():
    state, disagreements = _state(
        events=[_view(7, 10, 0), _view(7, 10, 500), _view(7, 11, 600), _view(7, 12, 700)],
        attempts=[_attempt(7, 800, 0.5), _attempt(7, 900, 0.8, attempt=2)],
        completions=[_completion(7, 10, 1, 0), _completion(7, 12, 2, 900)],
    )

    assert list(state.reset_index().columns) == ACTIVITY_STATE_COLUMNS
    assert len(state) == 3 and disagreements == 0
    lesson = state.loc[("7", 10)]
    assert lesson["status"] == "completed" and lesson["view_count"] == 2
    assert lesson["first_viewed_at"] < lesson["last_viewed_at"]
    assert state.loc[("7", 11), "status"] == "viewed"  # opened, not complete
    quiz = state.loc[("7", 12)]
    assert quiz["status"] == "passed" and quiz["status_source"] == "moodle_completion"
    assert quiz["attempt_count"] == 2
    assert quiz["last_score"] == 0.8 and quiz["best_score"] == 0.8
    assert quiz["pass_fraction"] == 0.7
    assert pd.isna(lesson["pass_fraction"])


def test_quiz_outcome_is_rebuilt_from_attempts_without_a_completion_record():
    state, _ = _state(
        attempts=[_attempt(7, 100, 0.5), _attempt(8, 100, 0.7), _attempt(9, 100, 0.9)]
    )

    assert state.loc[("7", 12), "status"] == "failed"
    assert state.loc[("8", 12), "status"] == "passed"  # the pass mark itself passes
    assert state.loc[("9", 12), "status"] == "passed"
    assert state["status_source"].eq("attempts").all()

    # No pass mark exported: an attempt is known, its outcome is not.
    unknown, _ = _state(
        attempts=[_attempt(7, 100, 0.5)], activities=_activities(pass_fraction=None)
    )
    assert unknown.loc[("7", 12), "status"] == "attempted"


def test_moodle_completion_wins_and_disagreements_are_counted():
    # Best score is below the pass mark, but Moodle says passed (e.g. an override).
    state, disagreements = _state(
        attempts=[_attempt(7, 100, 0.5)], completions=[_completion(7, 12, 2, 100)]
    )

    assert state.loc[("7", 12), "status"] == "passed"
    assert state.loc[("7", 12), "status_source"] == "moodle_completion"
    assert disagreements == 1


def test_no_activity_gives_an_empty_table():
    frame, disagreements = build_activity_state(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), _activities()
    )
    assert frame.empty and list(frame.columns) == ACTIVITY_STATE_COLUMNS
    assert disagreements == 0


def test_a_quiz_failed_before_the_cutoff_and_passed_after_it_is_failed_at_the_cutoff(
    tmp_path,
):
    """Moodle keeps one completion row per quiz, stamped with the later pass.

    At a cutoff between the two attempts that row is outside the snapshot, so
    the earlier failure would vanish unless it is rebuilt from the attempts.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _activities().to_csv(data_dir / "activity_catalog.csv", index=False)
    pd.DataFrame([_view(7, 10, 0), _view(7, 12, 50)]).assign(
        log_id=[1, 2], timestamp="2026-01-01 00:00:00"
    ).to_csv(data_dir / "learning_events.csv", index=False)
    pd.DataFrame([_attempt(7, 100, 0.5), _attempt(7, 300, 0.9, attempt=2)]).to_csv(
        data_dir / "quiz_attempts.csv", index=False
    )
    pd.DataFrame([_completion(7, 10, 1, 0), _completion(7, 12, 2, 300)]).to_csv(
        data_dir / "course_modules_completion.csv", index=False
    )

    def build(as_of=None):
        outputs = build_complete_learner_model(
            data_dir=data_dir,
            output_dir=tmp_path / f"out-{as_of}",
            knowledge_state_file=tmp_path / "none.csv",
            as_of=as_of,
        )
        return outputs["learner_activity_state"].set_index("course_module_id")

    now = build()
    assert now.loc[12, "status"] == "passed"
    assert now.loc[12, "status_source"] == "moodle_completion"
    assert now.loc[12, "attempt_count"] == 2

    between = build(as_of=T0 + 200)
    assert between.loc[12, "status"] == "failed"
    assert between.loc[12, "status_source"] == "attempts"
    assert between.loc[12, "attempt_count"] == 1
    assert between.loc[12, "best_score"] == 0.5
    assert between.loc[10, "status"] == "completed"

    before = build(as_of=T0 + 60)
    assert before.loc[12, "status"] == "viewed"  # opened, no finished attempt yet
    assert before.loc[12, "attempt_count"] == 0

    manifest = json.loads((tmp_path / "out-None" / "build_manifest.json").read_text())
    assert manifest["activity_state"]["quiz_status_disagreements"] == 0
    assert manifest["activity_state"]["quizzes_without_pass_mark"] is False
    assert manifest["output_counts"]["learner_activity_state"] == 2


def test_a_quiz_without_pass_mark_is_completed_by_an_attempt_when_its_role_says_so():
    practice = _activities(pass_fraction=None).assign(quiz_role=["", "", "practice"])
    # Live: Moodle's completion state "complete" (1) on a quiz without a pass mark.
    live, _ = _state(
        attempts=[_attempt(7, 100, 0.25)], completions=[_completion(7, 12, 1, 100)],
        activities=practice,
    )
    assert (live.loc[("7", 12), "status"], live.loc[("7", 12), "status_source"]) == (
        "completed", "moodle_completion",
    )
    # Rebuilt from attempts (as_of replay): the role gives the same answer.
    rebuilt, _ = _state(attempts=[_attempt(7, 100, 0.25)], activities=practice)
    assert (rebuilt.loc[("7", 12), "status"], rebuilt.loc[("7", 12), "status_source"]) == (
        "completed", "attempts",
    )
    # A check quiz (or a course without roles) keeps the pass-mark logic.
    check = _activities().assign(quiz_role=["", "", "check"])
    failed, _ = _state(attempts=[_attempt(7, 100, 0.25)], activities=check)
    assert failed.loc[("7", 12), "status"] == "failed"
