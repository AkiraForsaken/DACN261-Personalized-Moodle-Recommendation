"""Oracle AUC: the generator's own probability, scored on the gate's targets.

For simulated learners the generator's P(correct | every hidden learner and
item parameter) is the Bayes-optimal score: by the Neyman-Pearson lemma,
ranking by it gives an ROC curve that dominates any score computed from less
information, so a knowledge-tracing model's *expected* AUC cannot exceed the
oracle's on the same answers. This module computes that comparison so it can
be defended and rechecked:

1. **Same answers.** The deep model and BKT are scored on the held-out test
   targets (``phase.deep_test_targets``). Every target is tied to one Moodle
   answer (learner, position in ``event_index`` order), and every Moodle answer
   to one planned answer and its ``p_correct`` (``moodle_setup annotate-plan``).
2. **Checks** (all must pass, or the report says which failed):
   - ``plan_matches_moodle``: every planned answer is in Moodle with the same
     outcome, and Moodle holds no simulated answer the plan lacks;
   - ``targets_match_answers``: each target's question and response equal the
     Moodle answer it is tied to; the model's prediction dump has the same
     responses in the same order;
   - ``oracle_all_answers_reproduced``: the oracle AUC over all planned answers
     equals the value the simulator recorded when it made the plan;
   - ``gate_reproduced``: deep and BKT AUC recomputed here equal the gate record.
3. **Uncertainty.** 95 % intervals from a bootstrap over held-out *learners*
   (answers of one learner are not independent), for each AUC and for the
   paired gaps; and the oracle's own sampling spread: its AUC when outcomes are
   redrawn from its probabilities.

There is no oracle for real learners; this applies to simulated data only.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .phase import deep_test_targets

REPORT_NAME = "oracle_report"
DEFAULT_REPS = 2000
DEFAULT_SEED = 20261118
ANSWERS_FILE = "question_answer_events.csv"


def _auc(y, p):
    from sklearn.metrics import roc_auc_score

    y = np.asarray(y)
    if y.min() == y.max():
        return float("nan")
    return float(roc_auc_score(y, p))


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def find_annotated_plan(data_dir):
    """The newest plan in ``data_dir`` that carries p_correct and Moodle user ids."""
    from moodle_setup.plan import is_annotated

    candidates = sorted(Path(data_dir).glob("plan*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            if is_annotated(json.loads(path.read_text(encoding="utf-8"))):
                return path
        except (ValueError, KeyError):
            continue
    return None


def plan_answers(plan):
    """One row per planned answer: Moodle user, quiz, attempt, entry, outcome, p_correct."""
    rows = [
        (
            str(learner["moodle_userid"]), int(attempt["quiz_id"]), int(attempt["attempt_number"]),
            int(answer["entry_id"]), int(answer["correct"]), float(answer["p_correct"]),
        )
        for learner in plan["learners"]
        if learner.get("attempts")
        for attempt in learner["attempts"]
        for answer in attempt["answers"]
    ]
    return pd.DataFrame(
        rows, columns=["learner_id", "quiz_id", "attempt_number", "entry_id", "plan_correct", "p_correct"]
    )


def _moodle_answers(data_dir):
    events = pd.read_csv(Path(data_dir) / ANSWERS_FILE, dtype={"learner_id": "string"})
    return events.assign(
        learner_id=events["learner_id"].astype(str),
        entry_id=pd.to_numeric(events["question_bank_entry_id"], errors="coerce").astype("Int64"),
        quiz_id=pd.to_numeric(events["quiz_id"], errors="coerce").astype("Int64"),
        attempt_number=pd.to_numeric(events["attempt_number"], errors="coerce").astype("Int64"),
        event_index=pd.to_numeric(events["event_index"], errors="coerce").astype(int),
        correct=pd.to_numeric(events["correct"], errors="coerce").astype(int),
    )


def _join(planned, moodle):
    """Tie planned answers to Moodle answers; the plan_matches_moodle check."""
    keys = ["learner_id", "quiz_id", "attempt_number", "entry_id"]
    simulated = moodle[moodle["learner_id"].isin(set(planned["learner_id"]))]
    for name, frame in (("plan", planned), ("Moodle", simulated)):
        if frame.duplicated(keys).any():
            raise ValueError(f"{name} holds one question twice in the same attempt")
    joined = simulated.merge(planned.astype({k: "Int64" for k in keys[1:]}), on=keys, how="outer",
                             indicator=True)
    both = joined["_merge"].eq("both")
    check = {
        "planned_answers": int(len(planned)),
        "moodle_answers_of_planned_learners": int(len(simulated)),
        "matched": int(both.sum()),
        "only_in_plan": int(joined["_merge"].eq("right_only").sum()),
        "only_in_moodle": int(joined["_merge"].eq("left_only").sum()),
        "outcome_disagreements": int(
            (joined.loc[both, "correct"] != joined.loc[both, "plan_correct"]).sum()
        ),
    }
    check["passed"] = (
        check["only_in_plan"] == 0 and check["only_in_moodle"] == 0
        and check["outcome_disagreements"] == 0
    )
    return joined[both].drop(columns="_merge"), check


def _tie_targets(targets, answers, question_map):
    """Each target's Moodle answer: the learner's answers in event_index order, window by window."""
    question_of_item = dict(zip(question_map["item_id"].astype(str), question_map["questions"].astype(int)))
    by_learner = {
        learner: frame.sort_values("event_index").reset_index(drop=True)
        for learner, frame in answers.groupby("learner_id")
    }
    offsets, window_start, tied = {}, {}, []
    for target in targets.itertuples(index=False):
        learner = str(target.learner_id)
        if (learner, target.test_row) not in window_start:
            # A new window of this learner starts where the previous one ended.
            window_start[(learner, target.test_row)] = offsets.get(learner, 0)
        index = window_start[(learner, target.test_row)] + target.position
        offsets[learner] = max(offsets.get(learner, 0), index + 1)
        frame = by_learner.get(learner)
        row = frame.iloc[index] if frame is not None and index < len(frame) else None
        tied.append(
            (
                None if row is None else int(row["event_index"]),
                None if row is None else question_of_item.get(str(row["item_id"])),
                None if row is None else int(row["correct"]),
                None if row is None or pd.isna(row.get("p_correct")) else float(row["p_correct"]),
            )
        )
    tied = pd.DataFrame(tied, columns=["event_index", "answer_question", "answer_correct", "oracle"])
    result = pd.concat([targets.reset_index(drop=True), tied], axis=1)
    ok = (
        result["event_index"].notna()
        & result["answer_question"].eq(result["question"])
        & result["answer_correct"].eq(result["response"])
    )
    check = {
        "targets": int(len(result)),
        "tied_to_an_answer": int(result["event_index"].notna().sum()),
        "question_and_response_agree": int(ok.sum()),
        "with_oracle_probability": int(result["oracle"].notna().sum()),
    }
    check["passed"] = check["question_and_response_agree"] == check["targets"] == check[
        "with_oracle_probability"
    ]
    return result, check


def _bootstrap(targets, columns, reps, seed):
    """Percentile 95 % intervals over a resampling of learners, for AUCs and paired gaps."""
    rng = np.random.default_rng(seed)
    groups = [np.flatnonzero(mask) for mask in
              (targets["learner_id"].to_numpy() == lid for lid in targets["learner_id"].unique())]
    y = targets["response"].to_numpy()
    scores = {name: targets[name].to_numpy(dtype=float) for name in columns}
    draws = {name: [] for name in columns}
    for _ in range(reps):
        picked = rng.integers(0, len(groups), len(groups))
        index = np.concatenate([groups[i] for i in picked])
        for name in columns:
            draws[name].append(_auc(y[index], scores[name][index]))
    draws = {name: np.asarray(values) for name, values in draws.items()}

    def interval(values):
        values = values[~np.isnan(values)]
        return [round(float(np.percentile(values, 2.5)), 4), round(float(np.percentile(values, 97.5)), 4)]

    gaps = {
        f"{a}_minus_{b}": interval(draws[a] - draws[b])
        for a, b in (("deep", "bkt"), ("oracle", "deep"), ("oracle", "bkt"))
        if a in draws and b in draws
    }
    return {name: interval(values) for name, values in draws.items()}, gaps


def _redrawn_oracle(p, reps, seed):
    """The oracle's AUC when outcomes are redrawn from its own probabilities."""
    rng = np.random.default_rng(seed + 1)
    values = np.asarray([_auc(rng.random(len(p)) < p, p) for _ in range(reps)])
    return {
        "mean": round(float(np.nanmean(values)), 4),
        "interval_95": [round(float(np.nanpercentile(values, 2.5)), 4),
                        round(float(np.nanpercentile(values, 97.5)), 4)],
    }


def oracle_report(data_dir, kt_dir, plan_path, active_model_file=None, reps=DEFAULT_REPS,
                  seed=DEFAULT_SEED):
    """Compute the oracle comparison; return the report (nothing is written)."""
    from knowledge_tracing.evaluate import parse_predictions
    from knowledge_tracing.kt_common import (
        dataset_artifacts_dir,
        load_data_config,
        selected_model_path,
        use_course_kt_dir,
    )

    data_dir, kt_dir, plan_path = Path(data_dir), Path(kt_dir), Path(plan_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    planned = plan_answers(plan)
    joined, plan_check = _join(planned, _moodle_answers(data_dir))

    with use_course_kt_dir(kt_dir):
        data_config = load_data_config(config.DEEP_DATASET_NAME)
        targets = deep_test_targets(data_config)
        selection = json.loads(selected_model_path(config.DEEP_DATASET_NAME).read_text(encoding="utf-8"))
        predictions = (
            dataset_artifacts_dir(config.DEEP_DATASET_NAME) / "evaluation" / "predictions"
            / f"{selection['model_name']}_seed{selection['seed']}_test_predictions.txt"
        )
    truth, deep = parse_predictions(predictions)
    question_map = pd.read_csv(Path(data_config["mapping_dir"]) / "question_map.csv")
    targets, target_check = _tie_targets(
        targets, joined[["learner_id", "event_index", "item_id", "correct", "p_correct"]], question_map
    )
    target_check["prediction_dump_agrees"] = bool(
        len(truth) == len(targets) and np.array_equal(truth, targets["response"].to_numpy())
    )
    target_check["passed"] = target_check["passed"] and target_check["prediction_dump_agrees"]
    targets["deep"] = deep if len(deep) == len(targets) else np.nan

    recorded = (plan["manifest"].get("annotated") or {}).get(
        "oracle_auc_all_answers", plan["manifest"].get("ceiling_auc")
    )
    all_answers = round(_auc(planned["plan_correct"], planned["p_correct"]), 4)
    checks = {
        "plan_matches_moodle": plan_check,
        "targets_match_answers": target_check,
        "oracle_all_answers_reproduced": {
            "recorded": recorded, "recomputed": all_answers, "passed": recorded == all_answers,
        },
    }
    usable = targets.dropna(subset=["oracle", "deep"])
    y = usable["response"].to_numpy()
    auc = {name: round(_auc(y, usable[name]), 4) for name in ("oracle", "deep", "bkt")}
    gate = {}
    if active_model_file and Path(active_model_file).is_file():
        gate = json.loads(Path(active_model_file).read_text(encoding="utf-8")).get("gate", {})
    if gate:
        checks["gate_reproduced"] = {
            "recorded": {"deep": round(gate["deep"]["auc"], 4), "bkt": round(gate["bkt"]["auc"], 4),
                         "n_targets": gate.get("n_targets")},
            "recomputed": {"deep": auc["deep"], "bkt": auc["bkt"], "n_targets": int(len(usable))},
        }
        checks["gate_reproduced"]["passed"] = (
            checks["gate_reproduced"]["recorded"] == checks["gate_reproduced"]["recomputed"]
        )
    intervals, gaps = _bootstrap(usable, ["oracle", "deep", "bkt"], reps, seed)
    headroom = {
        name: round((auc[name] - 0.5) / (auc["oracle"] - 0.5), 3) if auc["oracle"] > 0.5 else None
        for name in ("deep", "bkt")
    }
    inputs = {
        "plan": plan_path,
        "answers": data_dir / ANSWERS_FILE,
        "test_file": Path(data_config["dpath"]) / data_config["test_file"],
        "predictions": predictions,
        "question_map": Path(data_config["mapping_dir"]) / "question_map.csv",
    }
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "population": "simulated",
        "model": {"name": selection["model_name"], "seed": selection["seed"],
                  "checkpoint_id": selection.get("checkpoint_id")},
        "n_targets": int(len(usable)),
        "n_test_learners": int(usable["learner_id"].nunique()),
        "auc": auc,
        "auc_ci_95": intervals,
        "paired_gap_ci_95": gaps,
        "share_of_oracle_headroom": headroom,
        "oracle_redrawn_outcomes": _redrawn_oracle(usable["oracle"].to_numpy(), reps, seed),
        "oracle_auc_all_answers": all_answers,
        "bootstrap": {"unit": "held-out learner", "reps": reps, "seed": seed},
        "checks": checks,
        "all_checks_passed": all(entry["passed"] for entry in checks.values()),
        "inputs": {name: {"path": str(path), "sha256": _sha256(path)} for name, path in inputs.items()},
    }


def to_markdown(report):
    auc, ci, gaps = report["auc"], report["auc_ci_95"], report["paired_gap_ci_95"]
    lines = [
        "# Oracle AUC on the gate's targets",
        "",
        f"Simulated learners only. {report['n_targets']:,} held-out targets of "
        f"{report['n_test_learners']} learners; model {report['model']['name']} "
        f"(seed {report['model']['seed']}). 95 % intervals: bootstrap over learners, "
        f"{report['bootstrap']['reps']} reps, seed {report['bootstrap']['seed']}.",
        "",
        "| Score | AUC | 95 % CI | share of oracle headroom above 0.5 |",
        "| --- | --- | --- | --- |",
        f"| oracle (generator probability) | {auc['oracle']} | {ci['oracle'][0]}-{ci['oracle'][1]} | 1 |",
        f"| {report['model']['name']} | {auc['deep']} | {ci['deep'][0]}-{ci['deep'][1]} | "
        f"{report['share_of_oracle_headroom']['deep']} |",
        f"| BKT | {auc['bkt']} | {ci['bkt'][0]}-{ci['bkt'][1]} | {report['share_of_oracle_headroom']['bkt']} |",
        "",
        "| Paired gap | 95 % CI |",
        "| --- | --- |",
        *(f"| {name.replace('_minus_', ' - ')} | {v[0]} to {v[1]} |" for name, v in gaps.items()),
        "",
        f"Oracle with outcomes redrawn from its own probabilities: mean "
        f"{report['oracle_redrawn_outcomes']['mean']}, 95 % "
        f"{report['oracle_redrawn_outcomes']['interval_95'][0]}-"
        f"{report['oracle_redrawn_outcomes']['interval_95'][1]} (its sampling spread on these items).",
        "",
        f"Generator AUC over all {report['checks']['plan_matches_moodle']['planned_answers']:,} "
        f"planned answers: {report['oracle_auc_all_answers']} (other answers; not comparable).",
        "",
        "## Checks",
        "",
        *(f"- {'PASS' if entry['passed'] else 'FAIL'} `{name}`: "
          + json.dumps({k: v for k, v in entry.items() if k != 'passed'})
          for name, entry in report["checks"].items()),
        "",
        "## Inputs",
        "",
        *(f"- `{name}`: {entry['path']} (sha256 `{entry['sha256'][:16]}...`)"
          for name, entry in report["inputs"].items()),
        "",
        "Interpretation: the generator's probability is the Bayes-optimal score for these "
        "simulated answers, so no model's expected AUC exceeds it on the same targets. It "
        "describes the simulator; there is no such bound for real learners.",
    ]
    return "\n".join(lines) + "\n"


def write_report(report, kt_dir, active_model_file=None):
    """Write oracle_report.json/.md next to the course model's evaluation; note it in the record."""
    from knowledge_tracing.kt_common import dataset_artifacts_dir, use_course_kt_dir

    with use_course_kt_dir(kt_dir):
        directory = dataset_artifacts_dir(config.DEEP_DATASET_NAME) / "evaluation"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{REPORT_NAME}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (directory / f"{REPORT_NAME}.md").write_text(to_markdown(report), encoding="utf-8")
    if active_model_file and Path(active_model_file).is_file():
        record = json.loads(Path(active_model_file).read_text(encoding="utf-8"))
        # Added after the decision; the gate itself is unchanged.
        record["oracle"] = {
            "n_targets": report["n_targets"],
            "oracle_auc": report["auc"]["oracle"],
            "oracle_auc_ci": report["auc_ci_95"]["oracle"],
            "all_checks_passed": report["all_checks_passed"],
            "report": str(directory / f"{REPORT_NAME}.md"),
        }
        if "simulator_ceiling_auc" in record:
            record["oracle_auc_all_answers"] = record.pop("simulator_ceiling_auc")
        Path(active_model_file).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return directory
