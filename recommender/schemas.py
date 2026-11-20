"""The recommendation contract: one row per learner and recommended activity.

``recommendations.csv`` is what the Moodle plugin imports and what the offline
evaluation scores. Every policy (the rules and the baselines) writes the same
columns.

    rank            1 = shown first; unique per learner. ``generate`` writes the
                    whole queue (every open activity); the first ``top_n``
                    are what the learner sees before doing anything.
    group           the slot the item competes for: remediation, prepare,
                    forward (baselines: baseline). The Moodle block refills the
                    list from the queue with these slot rules. ``contingency``
                    rows are a precomputed branch, shown only after the
                    learner fails the quiz in ``trigger_cmid``.
    trigger_cmid    contingency rows only: the quiz whose failure activates
                    the row (empty on every other row). Contingency rows come
                    after the queue; the block puts an activated branch first.
    reason_code     why this activity (see REASON_CODES)
    reason_text     the same, as a sentence for the learner
    basis           what the recommendation rests on:
                      evidence      the learner's own graded answers
                      prediction    a model estimate, no direct evidence
                      course_order  position in the course only
                      baseline      a non-personalised comparison policy
    evidence        JSON with the numbers behind the reason (for teachers)
"""

import json

import pandas as pd

RECOMMENDATION_COLUMNS = [
    "learner_id",
    "rank",
    "group",
    "trigger_cmid",
    "course_module_id",
    "activity_type",
    "activity_name",
    "kc_id",
    "kc_name",
    "reason_code",
    "reason_text",
    "basis",
    "evidence",
    "policy",
    "generated_at",
    "as_of",
    "estimator_phase",
    "source_population",
    "mapping_version",
    "recommender_version",
]

BASIS_EVIDENCE = "evidence"
BASIS_PREDICTION = "prediction"
BASIS_COURSE_ORDER = "course_order"
BASIS_BASELINE = "baseline"
BASES = (BASIS_EVIDENCE, BASIS_PREDICTION, BASIS_COURSE_ORDER, BASIS_BASELINE)

REVISE_BEFORE_RETRY = "revise_before_retry"
RETRY_QUIZ = "retry_quiz"
REVISIT_PREREQUISITE = "revisit_prerequisite"
REVIEW_WEAK_KC = "review_weak_kc"
PRACTICE_WEAK_KC = "practice_weak_kc"
PREPARE_PREDICTED_DIFFICULTY = "prepare_predicted_difficulty"
CONTINUE_COURSE = "continue_course"
START_COURSE = "start_course"
BASELINE_COURSE_ORDER = "baseline_course_order"
BASELINE_POPULARITY = "baseline_popularity"
BASELINE_RANDOM = "baseline_random"

# reason_code -> basis
REASON_CODES = {
    REVISE_BEFORE_RETRY: BASIS_EVIDENCE,
    RETRY_QUIZ: BASIS_EVIDENCE,
    REVISIT_PREREQUISITE: BASIS_EVIDENCE,
    REVIEW_WEAK_KC: BASIS_EVIDENCE,
    PRACTICE_WEAK_KC: BASIS_EVIDENCE,
    PREPARE_PREDICTED_DIFFICULTY: BASIS_PREDICTION,
    CONTINUE_COURSE: BASIS_COURSE_ORDER,
    START_COURSE: BASIS_COURSE_ORDER,
    BASELINE_COURSE_ORDER: BASIS_BASELINE,
    BASELINE_POPULARITY: BASIS_BASELINE,
    BASELINE_RANDOM: BASIS_BASELINE,
}
GROUP_REMEDIATION = "remediation"
GROUP_PREPARE = "prepare"
GROUP_FORWARD = "forward"
GROUP_BASELINE = "baseline"
GROUP_CONTINGENCY = "contingency"
# reason_code -> the slot it competes for in the list
REASON_GROUPS = {
    REVISE_BEFORE_RETRY: GROUP_REMEDIATION,
    RETRY_QUIZ: GROUP_REMEDIATION,
    REVISIT_PREREQUISITE: GROUP_REMEDIATION,
    REVIEW_WEAK_KC: GROUP_REMEDIATION,
    PRACTICE_WEAK_KC: GROUP_REMEDIATION,
    PREPARE_PREDICTED_DIFFICULTY: GROUP_PREPARE,
    CONTINUE_COURSE: GROUP_FORWARD,
    START_COURSE: GROUP_FORWARD,
    BASELINE_COURSE_ORDER: GROUP_BASELINE,
    BASELINE_POPULARITY: GROUP_BASELINE,
    BASELINE_RANDOM: GROUP_BASELINE,
}
REMEDIATION_CODES = (
    REVISE_BEFORE_RETRY,
    RETRY_QUIZ,
    REVISIT_PREREQUISITE,
    REVIEW_WEAK_KC,
    PRACTICE_WEAK_KC,
)

# What a contingency branch may hold: revise the quiz's lessons, revisit an
# earlier unit, retry the quiz.
CONTINGENCY_CODES = (REVISE_BEFORE_RETRY, REVISIT_PREREQUISITE, RETRY_QUIZ)


def validate_recommendations(frame, known_modules=None, known_learners=None, top_n=None):
    """Raise ValueError if ``frame`` breaks the contract; return it otherwise."""
    missing = [c for c in RECOMMENDATION_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"Recommendations are missing columns: {missing}")
    frame = frame[RECOMMENDATION_COLUMNS]
    if frame.empty:
        return frame
    required = ["learner_id", "rank", "course_module_id", "reason_code", "reason_text", "basis"]
    if frame[required].isna().any().any():
        raise ValueError(f"Recommendations need a value in each of {required}")
    if frame.duplicated(["learner_id", "rank"]).any():
        raise ValueError("A learner has two recommendations with the same rank")
    trigger = pd.to_numeric(frame["trigger_cmid"], errors="coerce")
    contingent = trigger.notna()
    # An activity appears once in the queue; a contingency branch may repeat it
    # (it replaces the queue row once activated), but once per trigger.
    if frame.assign(_trigger=trigger.fillna(0)).duplicated(
        ["learner_id", "course_module_id", "_trigger"]
    ).any():
        raise ValueError("A learner is recommended the same activity twice")
    ranks = frame.groupby("learner_id")["rank"]
    if not (ranks.min().eq(1) & ranks.max().eq(ranks.size())).all():
        raise ValueError("Ranks must run 1..n without gaps for every learner")
    if top_n is not None and ranks.size().gt(top_n).any():
        raise ValueError(f"A learner has more than {top_n} recommendations")
    if contingent.any():
        queue_end = frame.loc[~contingent].groupby("learner_id")["rank"].max()
        first_branch = frame.loc[contingent].groupby("learner_id")["rank"].min()
        if (first_branch <= queue_end.reindex(first_branch.index).fillna(0)).any():
            raise ValueError("Contingency rows must come after the queue")
    unknown = set(frame["reason_code"]) - set(REASON_CODES)
    if unknown:
        raise ValueError(f"Unknown reason codes: {sorted(unknown)}")
    expected_basis = frame["reason_code"].map(REASON_CODES)
    if not frame["basis"].eq(expected_basis).all():
        raise ValueError("A recommendation's basis does not match its reason code")
    expected_group = frame["reason_code"].map(REASON_GROUPS).where(~contingent, GROUP_CONTINGENCY)
    if not frame["group"].eq(expected_group).all():
        raise ValueError("A recommendation's group does not match its reason code")
    if not frame.loc[contingent, "reason_code"].isin(CONTINGENCY_CODES).all():
        raise ValueError("A contingency row must revise, revisit or retry")
    if known_modules is not None:
        stray = (
            set(frame["course_module_id"].astype(int)) | set(trigger.dropna().astype(int))
        ) - set(known_modules)
        if stray:
            raise ValueError(f"Recommended activities are not in the course: {sorted(stray)[:10]}")
    if known_learners is not None:
        stray = set(frame["learner_id"].astype(str)) - set(map(str, known_learners))
        if stray:
            raise ValueError(f"Recommendations for learners without a profile: {sorted(stray)[:10]}")
    for payload in frame["evidence"]:
        if not isinstance(json.loads(payload), dict):
            raise ValueError("evidence must be a JSON object")
    # A prediction-based item must say which estimator the prediction came from.
    prediction = frame["basis"].eq(BASIS_PREDICTION)
    if pd.to_numeric(
        frame.loc[prediction, "estimator_phase"], errors="coerce"
    ).isna().any():
        raise ValueError("A prediction-based recommendation needs its estimator phase")
    return frame
