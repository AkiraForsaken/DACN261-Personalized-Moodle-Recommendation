"""Oracle AUC: plan annotation (moodle_setup.plan) and the same-target report (knowledge_engine.oracle)."""

import json

import numpy as np
import pandas as pd
import pytest

from knowledge_engine.oracle import oracle_report, plan_answers
from knowledge_engine.simulator import simulate_plan
from moodle_setup.plan import PlanMismatch, annotate_plan, is_annotated

QUIZZES = [
    {"quiz_id": 1, "cmid": 11, "section": 1, "grade": 10, "gradepass": 7,
     "entries": [{"entry_id": e, "kc_id": f"KC{e % 2}"} for e in range(100, 104)]},
    {"quiz_id": 2, "cmid": 12, "section": 2, "grade": 10, "gradepass": 7,
     "entries": [{"entry_id": e, "kc_id": f"KC{2 + e % 2}"} for e in range(200, 204)]},
]
STRUCTURE = {"quizzes": QUIZZES, "lessons": {}, "overviews": {}, "startdate": 1_700_000_000}


def _old_plan(learners=6, seed=3):
    """A plan as written before p_correct existed."""
    plan, manifest = simulate_plan(QUIZZES, n_learners=learners, seed=seed, start_unix=1_700_000_000)
    for learner in plan:
        for attempt in learner["attempts"]:
            for answer in attempt["answers"]:
                answer.pop("p_correct")
    manifest.pop("parameters")  # older manifests did not record them
    return {"manifest": manifest, "learners": plan}


def _users(plan):
    return {learner["number"]: 500 + learner["number"] for learner in plan["learners"]}


# ------------------------------------------------------------------ annotation
def test_annotation_restores_the_generating_probabilities_of_an_old_plan():
    stored = _old_plan()
    annotated = annotate_plan(stored, STRUCTURE, _users(stored))

    assert is_annotated(annotated) and not is_annotated(stored)
    fresh, _ = simulate_plan(QUIZZES, n_learners=6, seed=3, start_unix=1_700_000_000)
    p_fresh = [a["p_correct"] for l in fresh for at in l["attempts"] for a in at["answers"]]
    p_annotated = plan_answers(annotated)["p_correct"].tolist()
    assert p_annotated == p_fresh
    assert {l["moodle_userid"] for l in annotated["learners"] if l["attempts"]} <= set(
        _users(stored).values()
    )


def test_annotation_refuses_a_plan_it_cannot_reproduce():
    stored = _old_plan()
    answer = next(a for l in stored["learners"] for at in l["attempts"] for a in at["answers"])
    answer["correct"] = 1 - answer["correct"]
    with pytest.raises(PlanMismatch, match="does not reproduce"):
        annotate_plan(stored, STRUCTURE, _users(stored))


def test_annotation_needs_every_answering_learner_in_moodle():
    stored = _old_plan()
    with pytest.raises(PlanMismatch, match="no SIM- account"):
        annotate_plan(stored, STRUCTURE, {})


# ------------------------------------------------------------------ the report
def _write_course(tmp_path, plan, deep_from="noise", flip_one=False):
    """Collector answers, a pyKT test file with every learner held out, predictions."""
    data_dir, kt_dir = tmp_path / "data", tmp_path / "kt"
    rng = np.random.default_rng(0)
    rows, tests, lines = [], [], []
    questions = {e["entry_id"]: i for i, e in enumerate(e for q in QUIZZES for e in q["entries"])}
    for learner in plan["learners"]:
        answers = [(at, a) for at in learner["attempts"] for a in at["answers"]]
        if len(answers) < 3:
            continue
        uid = str(learner["moodle_userid"])
        for index, (attempt, answer) in enumerate(answers, start=1):
            rows.append({
                "learner_id": uid, "event_index": index, "item_id": f"moodle-qbe:{answer['entry_id']}",
                "question_bank_entry_id": answer["entry_id"], "quiz_id": attempt["quiz_id"],
                "attempt_number": attempt["attempt_number"], "correct": answer["correct"],
            })
        qs = [questions[a["entry_id"]] for _, a in answers]
        rs = [a["correct"] for _, a in answers]
        tests.append({"fold": -1, "uid": len(tests), "learner_id": uid,
                      "questions": ",".join(map(str, qs)), "concepts": ",".join(map(str, qs)),
                      "responses": ",".join(map(str, rs)), "selectmasks": ",".join(["1"] * len(qs))})
        p = [a["p_correct"] for _, a in answers][1:]
        ps = p if deep_from == "oracle" else [float(v) for v in rng.random(len(p))]
        lines.append(repr([qs[1:], rs[1:], [], rs[1:], ps, [], 0.5, 0.5]))
    if flip_one:
        rows[1]["correct"] = 1 - rows[1]["correct"]
    data_dir.mkdir()
    pd.DataFrame(rows).to_csv(data_dir / "question_answer_events.csv", index=False)
    (kt_dir / "pykt").mkdir(parents=True)
    (kt_dir / "processed").mkdir()
    pd.DataFrame(tests).to_csv(kt_dir / "pykt" / "test_sequences.csv", index=False)
    pd.DataFrame({"item_id": [f"moodle-qbe:{e}" for e in questions], "questions": list(questions.values())}
                 ).to_csv(kt_dir / "processed" / "question_map.csv", index=False)
    (kt_dir / "data_config.json").write_text(json.dumps({"moodle_kt": {
        "dpath": "pykt", "mapping_dir": "processed", "test_file": "test_sequences.csv",
        "train_valid_file": "train_valid_sequences.csv", "num_q": len(questions), "num_c": len(questions),
    }}))
    evaluation = kt_dir / "artifacts" / "evaluation" / "predictions"
    evaluation.mkdir(parents=True)
    (evaluation / "akt_seed1_test_predictions.txt").write_text("\n".join(lines) + "\n")
    (kt_dir / "artifacts" / "selected_model.json").write_text(json.dumps(
        {"model_name": "akt", "seed": 1, "checkpoint_id": "test"}))
    return data_dir, kt_dir


def _annotated(learners=40):
    stored = _old_plan(learners=learners, seed=5)
    return annotate_plan(stored, STRUCTURE, _users(stored))


def test_the_report_ties_every_target_to_its_answer_and_passes_its_checks(tmp_path):
    plan = _annotated()
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    data_dir, kt_dir = _write_course(tmp_path, plan)

    report = oracle_report(data_dir, kt_dir, tmp_path / "plan.json", reps=50)

    assert report["all_checks_passed"], report["checks"]
    targets = report["checks"]["targets_match_answers"]
    assert targets["targets"] == targets["with_oracle_probability"] == report["n_targets"] > 0
    # Random "model" scores: far below the oracle.
    assert report["auc"]["oracle"] > report["auc"]["deep"]
    low, high = report["auc_ci_95"]["oracle"]
    assert low <= report["auc"]["oracle"] <= high


def test_a_model_that_is_the_oracle_has_no_gap(tmp_path):
    plan = _annotated()
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    data_dir, kt_dir = _write_course(tmp_path, plan, deep_from="oracle")

    report = oracle_report(data_dir, kt_dir, tmp_path / "plan.json", reps=50)

    assert report["auc"]["deep"] == report["auc"]["oracle"]
    assert report["share_of_oracle_headroom"]["deep"] == 1.0
    assert report["paired_gap_ci_95"]["oracle_minus_deep"] == [0.0, 0.0]


def test_an_answer_that_differs_from_the_plan_fails_the_check(tmp_path):
    plan = _annotated()
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    data_dir, kt_dir = _write_course(tmp_path, plan, flip_one=True)

    report = oracle_report(data_dir, kt_dir, tmp_path / "plan.json", reps=20)

    assert not report["checks"]["plan_matches_moodle"]["passed"]
    assert report["checks"]["plan_matches_moodle"]["outcome_disagreements"] == 1
    assert not report["all_checks_passed"]


def test_the_bootstrap_is_reproducible_under_its_seed(tmp_path):
    plan = _annotated()
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    data_dir, kt_dir = _write_course(tmp_path, plan)

    first = oracle_report(data_dir, kt_dir, tmp_path / "plan.json", reps=30, seed=9)
    again = oracle_report(data_dir, kt_dir, tmp_path / "plan.json", reps=30, seed=9)

    assert first["auc_ci_95"] == again["auc_ci_95"]
    assert first["paired_gap_ci_95"] == again["paired_gap_ci_95"]
