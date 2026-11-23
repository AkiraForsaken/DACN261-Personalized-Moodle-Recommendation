"""What each learner has done with each course activity.

One row per learner and activity the learner has touched (viewed, attempted or
completed) within the snapshot. An activity without a row has not been
started. The recommender reads this to know which quizzes are currently
failed and whether a lesson was read since the last attempt; the teacher view
shows it as the learner's progress through the course.

``status``

    viewed     opened, nothing more (a page not yet complete, a quiz not attempted)
    completed  an ungraded activity Moodle marks complete, or a quiz without
               a pass mark that is complete once attempted: Moodle's
               completion state "complete", or (rebuilt from attempts) a
               quiz whose role is practice, check-in, review or challenge
               (``quiz_role`` in the activity catalog)
    passed     a quiz (or other graded activity) whose pass mark was reached
    failed     attempted without reaching the pass mark
    attempted  a quiz attempted, outcome unknown (no pass mark, no completion
               record, no role saying an attempt completes it)

``completed`` and ``passed`` both mean "done"; ``DONE_STATUSES`` lists them.

``status_source`` says where a quiz's outcome comes from:

    moodle_completion  Moodle's own completion record inside the snapshot. It
                       is authoritative: it reflects the quiz's grading method
                       and any teacher override.
    attempts           reconstructed from the finished attempts in the
                       snapshot: best score against the quiz's pass mark. Used
                       when no completion record is inside the snapshot. With
                       an ``as_of`` cutoff that is the normal case for a quiz
                       failed before the cutoff and passed after it, whose
                       completion record carries the later time.

The frames passed in are already cut off (``temporal.truncate_frames``), so
this component is leakage-free at any ``as_of`` like the others.
"""

import pandas as pd

ACTIVITY_STATE_COLUMNS = [
    "learner_id",
    "course_module_id",
    "activity_type",
    "status",
    "status_source",
    "view_count",
    "first_viewed_at",
    "last_viewed_at",
    "attempt_count",
    "last_attempt_at",
    "last_score",
    "best_score",
    "pass_fraction",
    "completion_state",
    "completed_at",
]
STATUS_VIEWED = "viewed"
STATUS_COMPLETED = "completed"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_ATTEMPTED = "attempted"
ACTIVITY_STATUSES = (
    STATUS_VIEWED,
    STATUS_COMPLETED,
    STATUS_PASSED,
    STATUS_FAILED,
    STATUS_ATTEMPTED,
)
DONE_STATUSES = (STATUS_COMPLETED, STATUS_PASSED)
SOURCE_COMPLETION = "moodle_completion"
SOURCE_ATTEMPTS = "attempts"
SOURCE_VIEWS = "views"

# Moodle completion states (see collector/collect_completion.py).
_COMPLETE, _COMPLETE_PASS, _COMPLETE_FAIL, _COMPLETE_FAIL_HIDDEN = 1, 2, 3, 4
_VIEW_CATEGORY = "content_view"
# Quiz roles (collector.collect_activities.quiz_role) that have no pass mark
# and are complete after any finished attempt.
ATTEMPT_COMPLETES_ROLES = ("practice", "checkin", "review", "challenge")
_SCORE_TOLERANCE = 1e-9


def _iso(unix_seconds):
    """Unix seconds -> ISO-8601 UTC text (None for a missing time)."""
    if unix_seconds is None or pd.isna(unix_seconds):
        return None
    return pd.Timestamp(int(unix_seconds), unit="s", tz="UTC").isoformat()


def _empty(*value_columns):
    """A typed empty learner x activity frame, so merges keep their key dtypes."""
    return pd.DataFrame(
        {
            "learner_id": pd.Series(dtype=object),
            "course_module_id": pd.Series(dtype="int64"),
            **{column: pd.Series(dtype="float64") for column in value_columns},
        }
    )


def _views(events):
    """learner x activity: number of views and first/last view time."""
    needed = {"learner_id", "course_module_id", "timestamp_unix"}
    if events is None or events.empty or not needed <= set(events.columns):
        return _empty("view_count", "first", "last")
    views = events
    if "event_category" in views.columns:
        views = views[views["event_category"].astype("string").eq(_VIEW_CATEGORY)]
    views = views.assign(
        course_module_id=pd.to_numeric(views["course_module_id"], errors="coerce"),
        timestamp_unix=pd.to_numeric(views["timestamp_unix"], errors="coerce"),
    ).dropna(subset=["course_module_id", "timestamp_unix"])
    grouped = views.groupby(
        [views["learner_id"].astype(str), views["course_module_id"].astype("int64")]
    )["timestamp_unix"]
    return grouped.agg(view_count="size", first="min", last="max").reset_index()


def _attempts(quizzes, activities):
    """learner x quiz activity: finished attempts, last and best score."""
    empty = _empty("attempt_count", "last_attempt", "last_score", "best_score")
    needed = {"learner_id", "quiz_id", "normalized_score", "finish_time_unix"}
    if quizzes is None or quizzes.empty or not needed <= set(quizzes.columns):
        return empty
    if not {"course_module_id", "activity_type", "activity_id"} <= set(
        activities.columns
    ):
        return empty
    quiz_activities = activities[activities["activity_type"].astype(str).eq("quiz")]
    module_of_quiz = dict(
        zip(
            pd.to_numeric(quiz_activities["activity_id"], errors="coerce"),
            pd.to_numeric(quiz_activities["course_module_id"], errors="coerce"),
        )
    )
    attempts = quizzes.assign(
        learner_id=quizzes["learner_id"].astype(str),
        course_module_id=pd.to_numeric(quizzes["quiz_id"], errors="coerce").map(
            module_of_quiz
        ),
        score=pd.to_numeric(quizzes["normalized_score"], errors="coerce"),
        finished=pd.to_numeric(quizzes["finish_time_unix"], errors="coerce"),
    ).dropna(subset=["course_module_id", "finished"])
    if attempts.empty:
        return empty
    attempts["course_module_id"] = attempts["course_module_id"].astype("int64")
    attempts = attempts.sort_values(
        ["learner_id", "course_module_id", "finished"], kind="mergesort"
    )
    grouped = attempts.groupby(["learner_id", "course_module_id"])
    return grouped.agg(
        attempt_count=("finished", "size"),
        last_attempt=("finished", "max"),
        last_score=("score", "last"),
        best_score=("score", "max"),
    ).reset_index()


def _completions(completions):
    """learner x activity: Moodle's completion state and when it was set."""
    needed = {"learner_id", "course_module_id", "completion_state"}
    if completions is None or completions.empty or not needed <= set(
        completions.columns
    ):
        return _empty("completion_state", "completed")
    rows = pd.DataFrame(
        {
            "learner_id": completions["learner_id"].astype(str),
            "course_module_id": pd.to_numeric(
                completions["course_module_id"], errors="coerce"
            ),
            "completion_state": pd.to_numeric(
                completions["completion_state"], errors="coerce"
            ),
            "completed": (
                pd.to_numeric(completions["time_modified_unix"], errors="coerce")
                if "time_modified_unix" in completions.columns
                else float("nan")
            ),
        }
    ).dropna(subset=["course_module_id", "completion_state"])
    rows["course_module_id"] = rows["course_module_id"].astype("int64")
    # State 0 (incomplete) says nothing happened; it is not a row of its own.
    rows = rows[rows["completion_state"].gt(0)]
    return rows.drop_duplicates(["learner_id", "course_module_id"], keep="last")


def _quiz_status(completion_state, attempt_count, best_score, pass_fraction, role=""):
    """``(status, source)`` of one learner x quiz row."""
    if completion_state == _COMPLETE_PASS:
        return STATUS_PASSED, SOURCE_COMPLETION
    if completion_state in (_COMPLETE_FAIL, _COMPLETE_FAIL_HIDDEN):
        return STATUS_FAILED, SOURCE_COMPLETION
    if completion_state == _COMPLETE and attempt_count and pass_fraction is None:
        return STATUS_COMPLETED, SOURCE_COMPLETION
    if attempt_count:
        if pass_fraction is not None and best_score is not None:
            passed = best_score >= pass_fraction - _SCORE_TOLERANCE
            return (STATUS_PASSED if passed else STATUS_FAILED), SOURCE_ATTEMPTS
        if role in ATTEMPT_COMPLETES_ROLES:
            return STATUS_COMPLETED, SOURCE_ATTEMPTS
        return STATUS_ATTEMPTED, SOURCE_ATTEMPTS
    return STATUS_VIEWED, SOURCE_VIEWS


def build_activity_state(events, quizzes, completions, activities):
    """One row per learner and touched activity; see the module docstring.

    Returns ``(frame, quiz_status_disagreements)``. The second value counts
    quiz rows where Moodle's completion record and the outcome reconstructed
    from the attempts differ (Moodle's record is kept). A non-zero count means
    the reconstruction used under ``as_of`` does not match how the course
    grades its quizzes (for example a grading method other than "highest
    grade", or teacher overrides) and should be read with that in mind.
    """
    views, attempts, completed = _views(events), _attempts(quizzes, activities), _completions(completions)
    keys = ["learner_id", "course_module_id"]
    merged = views.merge(attempts, on=keys, how="outer").merge(
        completed, on=keys, how="outer"
    )
    if merged.empty:
        return pd.DataFrame(columns=ACTIVITY_STATE_COLUMNS), 0

    catalog = (
        activities.drop_duplicates("course_module_id")
        if "course_module_id" in activities.columns
        else pd.DataFrame(columns=["course_module_id"])
    )
    module_ids = pd.to_numeric(catalog.get("course_module_id"), errors="coerce")
    type_of = dict(zip(module_ids, catalog.get("activity_type", pd.Series(dtype=str))))
    pass_of = (
        dict(zip(module_ids, pd.to_numeric(catalog["pass_fraction"], errors="coerce")))
        if "pass_fraction" in catalog.columns
        else {}
    )
    role_of = (
        dict(zip(module_ids, catalog["quiz_role"].fillna("").astype(str)))
        if "quiz_role" in catalog.columns
        else {}
    )

    rows, disagreements = [], 0
    for record in merged.itertuples(index=False):
        module_id = int(record.course_module_id)
        activity_type = type_of.get(module_id)
        activity_type = None if pd.isna(activity_type) else str(activity_type)
        state = None if pd.isna(record.completion_state) else int(record.completion_state)
        attempt_count = 0 if pd.isna(record.attempt_count) else int(record.attempt_count)
        best = None if pd.isna(record.best_score) else float(record.best_score)
        last = None if pd.isna(record.last_score) else float(record.last_score)
        pass_fraction = pass_of.get(module_id)
        pass_fraction = (
            None if pass_fraction is None or pd.isna(pass_fraction) else float(pass_fraction)
        )
        if activity_type == "quiz" or attempt_count:
            role = role_of.get(module_id, "")
            status, source = _quiz_status(state, attempt_count, best, pass_fraction, role)
            if source == SOURCE_COMPLETION and attempt_count:
                rebuilt, _ = _quiz_status(None, attempt_count, best, pass_fraction, role)
                if rebuilt in (STATUS_PASSED, STATUS_FAILED) and rebuilt != status:
                    disagreements += 1
        elif state == _COMPLETE:
            status, source = STATUS_COMPLETED, SOURCE_COMPLETION
        elif state == _COMPLETE_PASS:  # another graded activity with a pass grade
            status, source = STATUS_PASSED, SOURCE_COMPLETION
        elif state in (_COMPLETE_FAIL, _COMPLETE_FAIL_HIDDEN):
            status, source = STATUS_FAILED, SOURCE_COMPLETION
        else:
            status, source = STATUS_VIEWED, SOURCE_VIEWS
        rows.append(
            {
                "learner_id": str(record.learner_id),
                "course_module_id": module_id,
                "activity_type": activity_type,
                "status": status,
                "status_source": source,
                "view_count": 0 if pd.isna(record.view_count) else int(record.view_count),
                "first_viewed_at": _iso(record.first),
                "last_viewed_at": _iso(record.last),
                "attempt_count": attempt_count,
                "last_attempt_at": _iso(record.last_attempt),
                "last_score": last,
                "best_score": best,
                "pass_fraction": pass_fraction if activity_type == "quiz" else None,
                "completion_state": state,
                "completed_at": _iso(record.completed) if state else None,
            }
        )
    frame = pd.DataFrame(rows, columns=ACTIVITY_STATE_COLUMNS)
    frame["completion_state"] = frame["completion_state"].astype("Int64")
    frame = frame.sort_values(keys, kind="mergesort").reset_index(drop=True)
    return frame, disagreements


def build_quiz_attempt_history(quizzes, activities):
    """One row per finished quiz attempt: when, the score, and whether it passed.

    ``quizzes`` is the collector's quiz_attempts frame already cut at the
    snapshot (``temporal.truncate_frames``), so nothing after ``as_of`` shows.
    ``passed`` compares the score with the quiz's pass mark (``pass_fraction``);
    it is empty for a quiz without a pass mark.
    """
    from .schemas import QUIZ_ATTEMPT_COLUMNS

    needed = {"learner_id", "quiz_id", "normalized_score", "finish_time_unix"}
    if (
        quizzes is None
        or quizzes.empty
        or not needed <= set(quizzes.columns)
        or not {"course_module_id", "activity_type", "activity_id"} <= set(activities.columns)
    ):
        return pd.DataFrame(columns=QUIZ_ATTEMPT_COLUMNS)
    quiz_activities = activities[activities["activity_type"].astype(str).eq("quiz")]
    ids = pd.to_numeric(quiz_activities["activity_id"], errors="coerce")
    module_of_quiz = dict(zip(ids, pd.to_numeric(quiz_activities["course_module_id"], errors="coerce")))
    name_of_quiz = dict(zip(ids, quiz_activities.get("activity_name", pd.Series(dtype=str))))
    pass_of_quiz = dict(
        zip(ids, pd.to_numeric(quiz_activities.get("pass_fraction"), errors="coerce"))
        if "pass_fraction" in quiz_activities
        else {}
    )
    quiz_ids = pd.to_numeric(quizzes["quiz_id"], errors="coerce")
    history = pd.DataFrame(
        {
            "learner_id": quizzes["learner_id"].astype(str),
            "course_module_id": quiz_ids.map(module_of_quiz),
            "quiz_name": quiz_ids.map(name_of_quiz),
            "attempt": pd.to_numeric(quizzes.get("attempt"), errors="coerce"),
            "finished": pd.to_numeric(quizzes["finish_time_unix"], errors="coerce"),
            "normalized_score": pd.to_numeric(quizzes["normalized_score"], errors="coerce"),
            "pass_fraction": quiz_ids.map(pass_of_quiz),
        }
    ).dropna(subset=["course_module_id", "finished"])
    if history.empty:
        return pd.DataFrame(columns=QUIZ_ATTEMPT_COLUMNS)
    history["course_module_id"] = history["course_module_id"].astype("int64")
    history["attempt"] = history["attempt"].astype("Int64")
    history = history.sort_values(["learner_id", "finished", "course_module_id"], kind="mergesort")
    history["finished_at"] = history["finished"].map(_iso)
    passed = history["normalized_score"] + _SCORE_TOLERANCE >= history["pass_fraction"]
    history["passed"] = passed.astype("Int64").where(history["pass_fraction"].notna())
    return history[QUIZ_ATTEMPT_COLUMNS].reset_index(drop=True)
