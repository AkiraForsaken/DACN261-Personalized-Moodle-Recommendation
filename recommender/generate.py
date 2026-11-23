"""Generate recommendations for every learner of one snapshot.

    python -m recommender generate

reads the learner-model build and the course structure, applies a policy to
each learner, validates the result and writes ``recommendations.csv`` with its
manifest. The default policy is ``rules`` (see ``rules.py`` and
``ranking.py``); the baselines exist for the evaluation.
"""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import baselines, config, schemas
from .inputs import load_inputs
from .ranking import group_of, order_queue, select
from .reasons import reason_text
from .rules import (
    contingency_candidates,
    forward_candidates,
    mark_stuck,
    remediation_candidates,
    stuck_quizzes,
)

POLICY_RULES = "rules"
POLICIES = (
    POLICY_RULES, "course_order", "popularity", "random", "content_based", "item_cf"
)
PERSONALISED_BASES = (schemas.BASIS_EVIDENCE, schemas.BASIS_PREDICTION)


def recommend(
    learner, inputs, policy=POLICY_RULES, top_n=config.TOP_N, context=None, full_queue=False
):
    """The ranked candidates of one learner under one policy.

    ``full_queue`` (rules only): every candidate, the top-N first (see
    ``ranking.order_queue``), then the contingency branches of the quizzes not
    yet passed; otherwise the top-N alone. Baselines always return their top-N.
    """
    context = context or {}
    if policy == POLICY_RULES:
        rank = order_queue if full_queue else select
        chosen = rank(
            remediation_candidates(learner, inputs.course, inputs.low_accuracy),
            forward_candidates(learner, inputs.course, config.MAX_PREPARE),
            top_n,
            config.MAX_REMEDIATION,
        )
        chosen = mark_stuck(chosen, learner, inputs.course)
        if full_queue:
            chosen = chosen + contingency_candidates(
                learner, inputs.course, inputs.low_accuracy
            )
        return chosen
    if policy == "course_order":
        return baselines.course_order(learner, inputs.course, top_n)
    if policy == "popularity":
        return baselines.popularity(
            learner, inputs.course, top_n, popularity=context.get("popularity")
        )
    if policy == "random":
        return baselines.random_open(
            learner, inputs.course, top_n, seed=context.get("seed", 0)
        )
    if policy == "content_based":
        return baselines.content_based(learner, inputs.course, top_n)
    if policy == "item_cf":
        return baselines.item_cf(
            learner, inputs.course, top_n, similarities=context.get("similarities")
        )
    raise ValueError(f"Unknown policy {policy!r}; choose one of {POLICIES}")


def policy_context(inputs, policy, seed=0):
    """What a policy needs that is computed once per snapshot, not per learner."""
    if policy == "popularity":
        return {"popularity": baselines.popularity_counts(inputs.learners)}
    if policy == "item_cf":
        return {"similarities": baselines.item_similarities(inputs.learners)}
    return {"seed": seed}


def recommend_all(
    inputs, policy=POLICY_RULES, top_n=config.TOP_N, generated_at=None, seed=0,
    full_queue=False,
):
    """Recommendations for every learner, as a contract-conforming DataFrame.

    With ``full_queue`` the rules write each learner's whole queue; rank
    1..top_n is still exactly the top-N list.
    """
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    course = inputs.course
    context = policy_context(inputs, policy, seed)
    rows = []
    for learner in inputs.learners.values():
        chosen = recommend(learner, inputs, policy, top_n, context, full_queue)
        for rank, candidate in enumerate(chosen, start=1):
            activity = course.by_module[candidate.module_id]
            rows.append(
                {
                    "learner_id": learner.learner_id,
                    "rank": rank,
                    "group": group_of(candidate),
                    "trigger_cmid": candidate.trigger_module_id,
                    "course_module_id": candidate.module_id,
                    "activity_type": activity.activity_type,
                    "activity_name": activity.name,
                    "kc_id": candidate.kc_id,
                    "kc_name": course.kc_names.get(candidate.kc_id),
                    "reason_code": candidate.reason_code,
                    "reason_text": reason_text(candidate, course),
                    "basis": schemas.REASON_CODES[candidate.reason_code],
                    "evidence": json.dumps(candidate.evidence, sort_keys=True),
                    "policy": policy,
                    "generated_at": generated_at,
                    "as_of": inputs.as_of,
                    "estimator_phase": learner.phase,
                    "source_population": learner.population,
                    "mapping_version": inputs.mapping_version,
                    "recommender_version": config.RECOMMENDER_VERSION,
                }
            )
    frame = pd.DataFrame(rows, columns=schemas.RECOMMENDATION_COLUMNS)
    frame["estimator_phase"] = frame["estimator_phase"].astype("Int64")
    frame["trigger_cmid"] = pd.to_numeric(frame["trigger_cmid"], errors="coerce").astype("Int64")
    return schemas.validate_recommendations(
        frame,
        known_modules=course.by_module,
        known_learners=inputs.learners,
        top_n=None if full_queue and policy == POLICY_RULES else top_n,
    )


def summarize(frame, inputs, top_n=config.TOP_N):
    """Counts for the manifest: how much of the output is actually personalised.

    Counted on the top-N a learner sees first, so the numbers mean the same
    with and without the full queue; ``queue`` describes the rest.
    """
    contingency = frame[frame["group"].eq(schemas.GROUP_CONTINGENCY)]
    queue = frame[~frame["group"].eq(schemas.GROUP_CONTINGENCY)]
    frame = queue[queue["rank"].le(top_n)]
    learner_ids = list(inputs.learners)
    with_any = set(frame["learner_id"])
    personalised = set(
        frame.loc[frame["basis"].isin(PERSONALISED_BASES), "learner_id"]
    )
    populations = Counter(
        str(learner.population) for learner in inputs.learners.values()
    )
    by_population = {}
    for population in sorted(populations):
        members = {
            lid for lid, learner in inputs.learners.items()
            if str(learner.population) == population
        }
        rows = frame[frame["learner_id"].isin(members)]
        by_population[population] = {
            "learners": len(members),
            "learners_with_recommendations": int(rows["learner_id"].nunique()),
            "learners_with_personalised_item": len(personalised & members),
            "rows_by_basis": {
                str(k): int(v) for k, v in rows["basis"].value_counts().items()
            },
        }
    return {
        "learners": len(learner_ids),
        "learners_with_recommendations": len(with_any),
        "learners_without_recommendations": len(learner_ids) - len(with_any),
        # A learner whose list holds at least one evidence- or prediction-based item.
        "learners_with_personalised_item": len(personalised),
        "rows": int(len(frame)),
        "rows_by_reason_code": {
            str(k): int(v) for k, v in frame["reason_code"].value_counts().items()
        },
        "rows_by_basis": {
            str(k): int(v) for k, v in frame["basis"].value_counts().items()
        },
        "by_population": by_population,
        # Learners with a check quiz at the retry cap (config.MAX_FAILED_CHECK_ATTEMPTS).
        "learners_at_retry_cap": sum(
            bool(stuck_quizzes(learner, inputs.course)) for learner in inputs.learners.values()
        ),
        "queue": {
            "rows": int(len(queue)),
            "mean_length": round(float(queue.groupby("learner_id").size().mean()), 2)
            if len(queue)
            else 0.0,
            "learners_with_empty_queue": len(learner_ids) - int(queue["learner_id"].nunique()),
        },
        # Precomputed "if this quiz is failed" branches (shown only after a fail).
        "contingency": {
            "rows": int(len(contingency)),
            "quizzes": int(contingency[["learner_id", "trigger_cmid"]].drop_duplicates().shape[0]),
            "rows_by_reason_code": {
                str(k): int(v) for k, v in contingency["reason_code"].value_counts().items()
            },
        },
    }


def generate(
    profile_dir,
    data_dir,
    output_dir,
    policy=POLICY_RULES,
    top_n=config.TOP_N,
    generated_at=None,
    seed=0,
    full_queue=True,
):
    """Write ``recommendations.csv`` and its manifest; return the frame.

    By default the rules write each learner's full queue, which Moodle
    consumes live; rank 1..top_n is the list before anything is done.
    """
    output_dir = Path(output_dir)
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    inputs = load_inputs(profile_dir, data_dir)
    full_queue = full_queue and policy == POLICY_RULES
    frame = recommend_all(inputs, policy, top_n, generated_at, seed, full_queue)

    output_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_dir / config.RECOMMENDATIONS_FILE, index=False)
    manifest = {
        "generated_at": generated_at,
        "as_of": inputs.as_of,
        "policy": policy,
        "recommender_version": config.RECOMMENDER_VERSION,
        "mapping_version": inputs.mapping_version,
        "configuration": {
            "top_n": top_n,
            "max_remediation": config.MAX_REMEDIATION,
            "max_prepare": config.MAX_PREPARE,
            "max_prerequisites": config.MAX_PREREQUISITES,
            "prerequisite_relation": config.PREREQUISITE_RELATION,
            "max_failed_check_attempts": config.MAX_FAILED_CHECK_ATTEMPTS,
            "challenge_min_answers": config.CHALLENGE_MIN_ANSWERS,
            "challenge_min_accuracy": config.CHALLENGE_MIN_ACCURACY,
            "max_challenge": config.MAX_CHALLENGE,
            "full_queue": full_queue,
            "low_observed_accuracy_threshold": inputs.low_accuracy,
            "note": "design choices, not calibrated on learner data",
        },
        "course": {
            "recommendable_activities": len(inputs.course.activities),
            "kcs_with_lesson": len(inputs.course.lesson_of_kc),
            # A KC's check quiz (named before quiz roles existed).
            "kcs_with_practice_quiz": len(inputs.course.quiz_of_kc),
            "kcs_with_low_stakes_practice": len(inputs.course.practice_of_kc),
            "kcs_with_challenge_quiz": len(inputs.course.challenge_of_kc),
            "quizzes_by_role": dict(
                Counter(
                    inputs.course.quiz_role(a.module_id)
                    for a in inputs.course.activities
                    if a.activity_type == "quiz"
                )
            ),
        },
        "summary": summarize(frame, inputs, top_n),
        "inputs": inputs.input_files,
        "semantics": {
            "basis": {
                "evidence": "rests on the learner's own graded answers",
                "prediction": "rests on a model estimate; no direct evidence on that KC",
                "course_order": "position in the course only; not personalised by knowledge",
                "baseline": "a comparison policy, never shown to learners",
            }
        },
    }
    (output_dir / config.MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return frame
