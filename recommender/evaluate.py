"""Offline replay: what would each policy have recommended at past moments?

    python -m recommender evaluate --cutoffs 6 --horizon-days 7

For each cutoff time T the whole pipeline is rebuilt as it would have looked
then (knowledge states up to T-1, learner model strictly before T), every
policy produces its top-N, and the lists are compared with what the learners
did in the following ``horizon``.

WHAT THE NUMBERS MEAN (and do not)

    hit / mrr / first_next_hit   agreement with what learners did next. On
                                 simulated learners the simulator's own habits
                                 ("revise after failing") are baked in, so a
                                 rule that mirrors them agrees by construction.
                                 This is not evidence of benefit.
    weak-KC targeting            later accuracy on KCs recommended for
                                 remediation vs the learner's other previously
                                 answered KCs. Shows whether flagged KCs really
                                 were weak. The strongest claim available offline.
    coverage / basis shares      how much of the output is personalised at all.
    stability                    overlap of a learner's list between cutoffs.

No learning gain is claimed: nobody followed these recommendations.

LEAKAGE CONTROLS

    estimator "bkt"     BKT has no trained weights, so states at T know nothing
                        after T. Scored on every learner.
    estimator "active"  a promoted deep model was trained on the full period,
                        so its weights have seen the future of its training
                        learners. It is scored only on its held-out test
                        learners (split_assignments.csv).
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, schemas
from .generate import POLICIES, PERSONALISED_BASES, recommend_all
from .inputs import load_inputs

DAY = 86_400
ACTIVITY_CATEGORIES = ("content_view", "quiz_attempt")


def _read(path, **kwargs):
    path = Path(path)
    return pd.read_csv(path, **kwargs) if path.is_file() else pd.DataFrame()


def load_activity_log(data_dir):
    """(learner_id, course_module_id, time): every lesson view and quiz attempt."""
    events = _read(Path(data_dir) / "learning_events.csv", dtype={"learner_id": str})
    if events.empty:
        return pd.DataFrame(columns=["learner_id", "course_module_id", "time"])
    if "event_category" in events.columns:
        events = events[events["event_category"].isin(ACTIVITY_CATEGORIES)]
    log = pd.DataFrame(
        {
            "learner_id": events["learner_id"].astype(str),
            "course_module_id": pd.to_numeric(events["course_module_id"], errors="coerce"),
            "time": pd.to_numeric(events["timestamp_unix"], errors="coerce"),
        }
    ).dropna()
    log["course_module_id"] = log["course_module_id"].astype(int)
    return log.sort_values(["learner_id", "time"], kind="mergesort").reset_index(drop=True)


def load_answers(data_dir):
    answers = _read(
        Path(data_dir) / "question_answer_events.csv",
        dtype={"learner_id": str, "kc_id": str},
    )
    if answers.empty:
        return pd.DataFrame(columns=["learner_id", "kc_id", "time", "correct"])
    return pd.DataFrame(
        {
            "learner_id": answers["learner_id"],
            "kc_id": answers["kc_id"],
            "time": pd.to_numeric(answers["timestamp"], errors="coerce"),
            "correct": pd.to_numeric(answers["correct"], errors="coerce"),
        }
    ).dropna()


def choose_cutoffs(log, n_cutoffs):
    """Cutoff times spread evenly over the middle of the observed activity."""
    if log.empty or n_cutoffs < 1:
        return []
    quantiles = np.linspace(0.2, 0.8, n_cutoffs) if n_cutoffs > 1 else [0.5]
    return sorted({int(t) for t in np.quantile(log["time"].to_numpy(), quantiles)})


def next_activities(log, cutoff, horizon_seconds):
    """learner_id -> modules touched in [cutoff, cutoff + horizon), in order."""
    window = log[(log["time"] >= cutoff) & (log["time"] < cutoff + horizon_seconds)]
    result = {}
    for learner_id, group in window.groupby("learner_id", sort=False):
        result[learner_id] = list(dict.fromkeys(group["course_module_id"].tolist()))
    return result


def agreement_metrics(recommendations, upcoming):
    """Hit, MRR and first-next hit over learners who were active in the horizon."""
    lists = {
        learner_id: group.sort_values("rank")["course_module_id"].astype(int).tolist()
        for learner_id, group in recommendations.groupby("learner_id", sort=False)
    }
    hits, reciprocal, first_hits, evaluated = 0, 0.0, 0, 0
    for learner_id, done in upcoming.items():
        recommended = lists.get(learner_id)
        if not recommended:
            continue
        evaluated += 1
        done_set = set(done)
        ranks = [rank for rank, module in enumerate(recommended, 1) if module in done_set]
        if ranks:
            hits += 1
            reciprocal += 1.0 / ranks[0]
        first_hits += done[0] in recommended
    return {
        "active_learners": evaluated,
        "hit": hits,
        "reciprocal_rank_sum": reciprocal,
        "first_next_hit": first_hits,
    }


def targeting_metrics(recommendations, answers, cutoff):
    """Later answers on KCs flagged for remediation vs other known KCs.

    Per learner with a remediation item: the KCs named by those items are
    "targeted"; the learner's other KCs with answers before the cutoff are the
    comparison. Counted are all answers at or after the cutoff. If the flags
    carry information, accuracy on targeted KCs is lower.

    The same is done for prediction-based items against the learner's other
    KCs without earlier answers.
    """
    before = answers[answers["time"] < cutoff]
    after = answers[answers["time"] >= cutoff]
    known = before.groupby("learner_id")["kc_id"].agg(set).to_dict()
    result = {
        key: 0
        for key in (
            "targeted_answers", "targeted_correct", "other_answers", "other_correct",
            "predicted_answers", "predicted_correct", "unseen_answers", "unseen_correct",
        )
    }
    flagged = recommendations[recommendations["kc_id"].notna()]
    remediation = (
        flagged[flagged["reason_code"].isin(schemas.REMEDIATION_CODES)]
        .groupby("learner_id")["kc_id"].agg(set).to_dict()
    )
    predicted = (
        flagged[flagged["basis"].eq(schemas.BASIS_PREDICTION)]
        .groupby("learner_id")["kc_id"].agg(set).to_dict()
    )
    for learner_id, group in after.groupby("learner_id", sort=False):
        seen = known.get(learner_id, set())
        targets = remediation.get(learner_id)
        if targets:
            is_target = group["kc_id"].isin(targets)
            is_other = group["kc_id"].isin(seen - targets)
            result["targeted_answers"] += int(is_target.sum())
            result["targeted_correct"] += int(group.loc[is_target, "correct"].sum())
            result["other_answers"] += int(is_other.sum())
            result["other_correct"] += int(group.loc[is_other, "correct"].sum())
        guesses = predicted.get(learner_id)
        if guesses:
            is_guess = group["kc_id"].isin(guesses)
            is_unseen = ~group["kc_id"].isin(seen | guesses)
            result["predicted_answers"] += int(is_guess.sum())
            result["predicted_correct"] += int(group.loc[is_guess, "correct"].sum())
            result["unseen_answers"] += int(is_unseen.sum())
            result["unseen_correct"] += int(group.loc[is_unseen, "correct"].sum())
    return result


def coverage_metrics(recommendations, learner_ids):
    personalised = recommendations[recommendations["basis"].isin(PERSONALISED_BASES)]
    counts = recommendations["basis"].value_counts()
    return {
        "learners": len(learner_ids),
        "learners_with_list": int(recommendations["learner_id"].nunique()),
        "learners_with_personalised_item": int(personalised["learner_id"].nunique()),
        "rows": int(len(recommendations)),
        **{f"rows_{basis}": int(counts.get(basis, 0)) for basis in schemas.BASES},
    }


def stability(previous, current):
    """Mean Jaccard overlap of each learner's list with the previous cutoff's."""
    if previous is None:
        return None, 0
    earlier = previous.groupby("learner_id")["course_module_id"].agg(set).to_dict()
    overlaps = []
    for learner_id, modules in current.groupby("learner_id")["course_module_id"].agg(set).items():
        old = earlier.get(learner_id)
        if old:
            overlaps.append(len(old & modules) / len(old | modules))
    return (float(np.mean(overlaps)) if overlaps else None), len(overlaps)


def _held_out_learners(split_file):
    if split_file is None:
        return None
    split = _read(split_file, dtype={"learner_id": str})
    if split.empty or not {"learner_id", "split"} <= set(split.columns):
        return None
    return set(split.loc[split["split"].eq("test"), "learner_id"])


def _snapshot(data_dir, work_dir, cutoff, force_estimator, active_model_file):
    """Rebuild knowledge states and the learner model as of ``cutoff``."""
    from knowledge_engine.estimate import estimate
    from learner_model.builder import build_complete_learner_model

    states_dir, profile_dir = work_dir / "states", work_dir / "profiles"
    # The engine's cutoff is inclusive, the collector frames' strict: T-1 and T
    # describe the same instant.
    estimate(
        data_dir,
        states_dir,
        as_of=cutoff - 1,
        active_model_file=active_model_file,
        force_estimator=force_estimator,
    )
    build_complete_learner_model(
        data_dir=data_dir,
        output_dir=profile_dir,
        knowledge_state_file=states_dir / "learner_kc_state.csv",
        as_of=cutoff,
    )
    return load_inputs(profile_dir, data_dir)


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def evaluate(
    data_dir,
    output_dir,
    n_cutoffs=6,
    horizon_days=7.0,
    top_n=config.TOP_N,
    estimators=("bkt", "active"),
    policies=POLICIES,
    work_dir=None,
    split_file=None,
    active_model_file=None,
):
    """Run the replay; write metrics.csv, report.md and manifest.json.

    ``active_model_file`` is the course's estimator decision (None: BKT only);
    ``split_file`` the deep model's learner split, which marks its held-out
    learners (default: the course's ``kt/processed/split_assignments.csv``).
    """
    from knowledge_engine.phase import course_kt_dir, load_active_model

    data_dir, output_dir = Path(data_dir), Path(output_dir)
    log, answers = load_activity_log(data_dir), load_answers(data_dir)
    cutoffs = choose_cutoffs(log, n_cutoffs)
    if not cutoffs:
        raise ValueError(f"No learner activity in {data_dir}; nothing to replay")
    horizon = int(horizon_days * DAY)
    active = load_active_model(active_model_file)
    if split_file is None and active_model_file is not None:
        split_file = course_kt_dir(active_model_file) / "processed" / "split_assignments.csv"

    notes, variants = [], []
    for estimator in estimators:
        if estimator == "bkt":
            variants.append(("bkt", "bkt", None))
            continue
        if active.get("phase") != 3:
            notes.append(
                "Estimator 'active' skipped: no deep model is promoted, so it "
                "would equal the BKT variant."
            )
            continue
        held_out = _held_out_learners(split_file)
        if not held_out:
            notes.append(
                f"Estimator 'active' skipped: no held-out learners found in {split_file}; "
                "scoring a trained model on its own training learners would leak the future."
            )
            continue
        variants.append(("active", None, held_out))

    temp = None
    if work_dir is None:
        temp = tempfile.TemporaryDirectory(prefix="recommender_eval_")
        work_dir = Path(temp.name)
    rows = []
    try:
        for name, force_estimator, scope in variants:
            previous = {}
            for cutoff in cutoffs:
                inputs = _snapshot(
                    data_dir,
                    Path(work_dir) / name / str(cutoff),
                    cutoff,
                    force_estimator,
                    active_model_file,
                )
                upcoming = next_activities(log, cutoff, horizon)
                learner_ids = [
                    lid for lid in inputs.learners if scope is None or lid in scope
                ]
                for policy in policies:
                    frame = recommend_all(inputs, policy, top_n, seed=cutoff)
                    frame = frame[frame["learner_id"].isin(learner_ids)]
                    scoped_upcoming = {
                        lid: done for lid, done in upcoming.items() if lid in set(learner_ids)
                    }
                    overlap, compared = stability(previous.get(policy), frame)
                    previous[policy] = frame
                    rows.append(
                        {
                            "estimator": name,
                            "cutoff": cutoff,
                            "cutoff_utc": pd.Timestamp(cutoff, unit="s", tz="UTC").isoformat(),
                            "policy": policy,
                            **coverage_metrics(frame, learner_ids),
                            **agreement_metrics(frame, scoped_upcoming),
                            **targeting_metrics(
                                frame, answers[answers["learner_id"].isin(learner_ids)], cutoff
                            ),
                            "stability_jaccard": overlap,
                            "stability_learners": compared,
                        }
                    )
    finally:
        if temp is not None:
            temp.cleanup()

    metrics = pd.DataFrame(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output_dir / "metrics.csv", index=False)
    summary = summarize(metrics)
    summary.to_csv(output_dir / "summary.csv", index=False)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "recommender_version": config.RECOMMENDER_VERSION,
        "cutoffs": cutoffs,
        "horizon_days": horizon_days,
        "top_n": top_n,
        "policies": list(policies),
        "estimators": [name for name, _, _ in variants],
        "held_out_learners": {
            name: (None if scope is None else len(scope)) for name, _, scope in variants
        },
        "active_model": {
            key: active.get(key) for key in ("phase", "estimator", "training_population")
        },
        "notes": notes,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = render_report(summary, manifest)
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    return report


def summarize(metrics):
    """Pool the per-cutoff counts into one row per estimator and policy."""
    if metrics.empty:
        return pd.DataFrame()
    rows = []
    for (estimator, policy), group in metrics.groupby(["estimator", "policy"], sort=False):
        total = group.sum(numeric_only=True)
        stable = group.dropna(subset=["stability_jaccard"])
        rows.append(
            {
                "estimator": estimator,
                "policy": policy,
                "cutoffs": int(group["cutoff"].nunique()),
                "learners": int(group["learners"].max()),
                "active_learner_cutoffs": int(total["active_learners"]),
                "hit_at_n": _rate(total["hit"], total["active_learners"]),
                "mrr": _rate(total["reciprocal_rank_sum"], total["active_learners"]),
                "first_next_hit": _rate(total["first_next_hit"], total["active_learners"]),
                "share_learners_personalised": _rate(
                    total["learners_with_personalised_item"], total["learners"]
                ),
                "share_rows_evidence": _rate(total["rows_evidence"], total["rows"]),
                "share_rows_prediction": _rate(total["rows_prediction"], total["rows"]),
                "share_rows_course_order": _rate(total["rows_course_order"], total["rows"]),
                "targeted_kc_later_accuracy": _rate(
                    total["targeted_correct"], total["targeted_answers"]
                ),
                "other_kc_later_accuracy": _rate(total["other_correct"], total["other_answers"]),
                "targeted_answers": int(total["targeted_answers"]),
                "predicted_kc_later_accuracy": _rate(
                    total["predicted_correct"], total["predicted_answers"]
                ),
                "unseen_kc_later_accuracy": _rate(
                    total["unseen_correct"], total["unseen_answers"]
                ),
                "predicted_answers": int(total["predicted_answers"]),
                "stability_jaccard": (
                    round(
                        float(
                            np.average(
                                stable["stability_jaccard"], weights=stable["stability_learners"]
                            )
                        ),
                        4,
                    )
                    if len(stable) and stable["stability_learners"].sum()
                    else None
                ),
            }
        )
    return pd.DataFrame(rows)


def _table(frame, columns):
    def cell(value):
        return "–" if value is None or (isinstance(value, float) and np.isnan(value)) else str(value)

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in frame[columns].itertuples(index=False):
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return lines


def render_report(summary, manifest):
    lines = [
        "# Recommender offline replay",
        "",
        f"- Generated: `{manifest['generated_at']}`; recommender `{manifest['recommender_version']}`",
        f"- {len(manifest['cutoffs'])} cutoffs, horizon {manifest['horizon_days']} days, "
        f"top {manifest['top_n']}",
        f"- Active estimator: phase {manifest['active_model'].get('phase')}, trained on "
        f"`{manifest['active_model'].get('training_population')}` learners",
        *[f"- Note: {note}" for note in manifest["notes"]],
        "",
        "Learners in this replay did not see any recommendation. Nothing below is",
        "evidence that recommendations help; simulated learners describe the simulator.",
        "",
    ]
    if summary.empty:
        return "\n".join(lines + ["No estimator variant could be evaluated.", ""])
    lines += [
        "## Agreement with what learners did next",
        "",
        "Share of active learner-cutoffs where a recommended activity was among the",
        "learner's next activities (`hit_at_n`), its reciprocal rank (`mrr`), and where",
        "the learner's very next activity was in the list (`first_next_hit`). The",
        "simulator makes learners revise after failing and move forward in course",
        "order, so policies built on the same habits agree by construction.",
        "",
        *_table(summary, ["estimator", "policy", "active_learner_cutoffs", "hit_at_n", "mrr", "first_next_hit"]),
        "",
        "## Were the flagged KCs really weak?",
        "",
        "Later accuracy on KCs named by remediation items against the learner's other",
        "previously answered KCs, and on KCs named by prediction-based items against",
        "the learner's other not-yet-answered KCs. Lower on the flagged side means the",
        "flags carry information. Baselines flag no KC.",
        "",
        *_table(
            summary[summary["policy"].eq("rules")],
            [
                "estimator", "targeted_kc_later_accuracy", "other_kc_later_accuracy",
                "targeted_answers", "predicted_kc_later_accuracy",
                "unseen_kc_later_accuracy", "predicted_answers",
            ],
        ),
        "",
        "## How much is personalised, and how stable",
        "",
        *_table(
            summary,
            [
                "estimator", "policy", "share_learners_personalised", "share_rows_evidence",
                "share_rows_prediction", "share_rows_course_order", "stability_jaccard",
            ],
        ),
        "",
        "## Limits",
        "",
        "- Rules, thresholds and slot sizes are design choices, not calibrated.",
        "- The 'active' estimator is scored on its held-out learners only; BKT on all.",
        "- Flagged KCs are chosen for low past accuracy, so part of their lower later",
        "  accuracy is the KC simply being harder for everyone, not this learner's gap.",
        "- Any effect of lessons on simulated answers is a generator assumption.",
        "",
    ]
    return "\n".join(lines)
