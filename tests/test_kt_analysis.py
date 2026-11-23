"""Per-answer analysis of KT prediction dumps, and the validation-only search."""

import json
import random
from pathlib import Path

import pandas as pd
import pytest

from knowledge_tracing.analysis import (
    attach_predictions,
    auc_by,
    position_bin,
    scored_targets,
    training_curve,
)
from knowledge_tracing.tune import sample_trials

ROOT = Path(__file__).resolve().parents[1]


def _workspace(tmp_path):
    kt = tmp_path / "kt"
    (kt / "pykt").mkdir(parents=True)
    (kt / "processed").mkdir()
    pd.DataFrame(
        {"item_id": ["q0", "q1", "q2", "q3"], "questions": [0, 1, 2, 3],
         "kc_id": ["a", "a", "b", "b"], "concepts": [0, 0, 1, 1],
         "kc_dimension": ["grammar", "grammar", "reading", "reading"]}
    ).to_csv(kt / "processed" / "question_map.csv", index=False)
    pd.DataFrame({"kc_id": ["a", "b"], "concepts": [0, 1],
                  "kc_dimension": ["grammar", "reading"]}).to_csv(
        kt / "processed" / "concept_map.csv", index=False)
    pd.DataFrame(
        {"fold": [0], "uid": [9], "questions": ["0,1,-1"], "concepts": ["0,0,-1"],
         "responses": ["1,0,-1"], "selectmasks": ["1,1,-1"]}
    ).to_csv(kt / "pykt" / "train_valid_sequences.csv", index=False)
    pd.DataFrame(
        {"fold": [-1, -1], "uid": [1, 2],
         "questions": ["0,2,3,-1", "1,3,-1,-1"], "concepts": ["0,1,1,-1", "0,1,-1,-1"],
         "responses": ["1,0,1,-1", "0,1,-1,-1"], "selectmasks": ["1,1,1,-1", "1,1,-1,-1"]}
    ).to_csv(kt / "pykt" / "test_sequences.csv", index=False)
    return kt


def test_targets_align_with_dump_and_mark_unseen_items(tmp_path):
    kt = _workspace(tmp_path)
    t = scored_targets(kt)
    assert list(t["position"]) == [2, 3, 2]
    assert list(t["item_seen_in_train"]) == [False, False, False]
    assert list(t["kc_dimension"]) == ["reading", "reading", "reading"]
    dump = tmp_path / "m.txt"
    dump.write_text(
        "[[0, 1], [1, 0], [1, 1], [0, 1], [0.2, np.float64(0.9)], [0, 1], nan, 1.0]\n"
        "[[0], [0], [1], [1], [0.7], [1], nan, 1.0]\n"
    )
    df = attach_predictions(t, dump, "m")
    assert df["m"].tolist() == [0.2, 0.9, 0.7]
    df["bin"] = position_bin(df["position"])
    assert set(auc_by(df, ["m"], "bin")["n"]) == {3}


def test_misaligned_dump_is_refused(tmp_path):
    kt = _workspace(tmp_path)
    dump = tmp_path / "bad.txt"
    dump.write_text(
        "[[0, 1], [1, 0], [1, 1], [1, 1], [0.2, 0.9], [0, 1], nan, 1.0]\n"
        "[[0], [0], [1], [1], [0.7], [1], nan, 1.0]\n"
    )
    with pytest.raises(ValueError, match="does not align"):
        attach_predictions(scored_targets(kt), dump, "m")


def test_training_curve_parses_pykt_log(tmp_path):
    log = tmp_path / "train.log"
    log.write_text(
        "Epoch: 1, validauc: 0.61, validacc: 0.6, best epoch: 1, best auc: 0.61, "
        "train loss: 0.68, emb_type: qid\n"
        "Epoch: 2, validauc: 0.64, validacc: 0.6, best epoch: 2, best auc: 0.64, "
        "train loss: 0.66, emb_type: qid\n"
    )
    c = training_curve(log)
    assert c["epoch"].tolist() == [1, 2] and c["valid_auc"].tolist() == [0.61, 0.64]


def test_search_reproduces_the_curated_english_trials():
    """tune.py samples exactly the trials the curated notebook search ran."""
    curated = ROOT / "knowledge_tracing/artifacts/tuning/tuning_results.csv"
    if not curated.exists():
        pytest.skip("curated tuning results not present")
    base = json.loads((ROOT / "knowledge_tracing/configs/model_configs.json").read_text())
    df = pd.read_csv(curated)
    for model in ("dkt", "sakt", "akt"):
        trials = sample_trials(model, 8, random.Random(0))
        for i, (m, t) in enumerate(trials):
            row = df[(df["model"] == model) & (df["trial"] == i)].iloc[0]
            assert json.loads(row["model_config"]) == {**base[model]["model_config"], **m}
            assert json.loads(row["training"]) == {**base[model]["training"], **t}


def test_score_saved_dump_matches_the_dump(tmp_path):
    """--from-dumps scores a run from its saved dump and run_summary, without a checkpoint."""
    from knowledge_tracing.evaluate import score_saved_dump

    seed_dir = tmp_path / "dkt" / "seed42"
    seed_dir.mkdir(parents=True)
    (seed_dir / "run_summary.json").write_text(
        json.dumps({"seed": 42, "best_epoch": 3, "valid_auc": 0.7}))
    pred = tmp_path / "evaluation" / "predictions"
    pred.mkdir(parents=True)
    # [qs, rs, ds, ts, ps, prelabels, auc, acc]; numpy-2 scalar repr as written on Colab
    (pred / "dkt_seed42_test_predictions.txt").write_text(
        "[[0, 0, 1], [1, 0, 1], [0, 1], [0, 1], [0.2, 0.9], [0, 1], np.float64(1.0), np.float64(1.0)]\n"
        "[[1, 1], [1, 0], [1], [1], [0.4], [0], nan, np.float64(0.0)]\n")
    r = score_saved_dump("dkt", seed_dir, tmp_path / "evaluation")
    assert r["n_targets"] == 3 and r["seed"] == 42 and r["valid_auc"] == 0.7
    assert r["auc"] == pytest.approx(1.0)  # targets 0,1,1 with p .2,.9,.4
    assert r["accuracy"] == pytest.approx(2 / 3)

    with pytest.raises(SystemExit):
        score_saved_dump("sakt", seed_dir, tmp_path / "evaluation")
