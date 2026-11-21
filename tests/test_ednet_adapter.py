"""EdNet-KT1 adapter (benchmark ETL) and the BKT grid fit."""

import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from knowledge_engine.bkt import BKTParams, bkt_step, fit_bkt_params
from knowledge_tracing.datasets import ednet_kt1
from knowledge_tracing.prepare_data import (
    REQUIRED_COLUMNS,
    build_maps,
    filter_answers,
    sort_chronologically,
)

QUESTIONS = """question_id,bundle_id,explanation_id,correct_answer,part,tags,deployed_at
q1,b1,e1,b,1,1;2;179,0
q2,b2,e2,a,2,1,0
q3,b3,e3,c,5,7;9,0
q4,b4,e4,d,5,-1,0
q5,b5,e5,a,5,1,0
"""


def _user_csv(rows):
    head = "timestamp,solving_id,question_id,user_answer,elapsed_time\n"
    return head + "".join(f"{t},{s},{q},{a},{e}\n" for t, s, q, a, e in rows)


@pytest.fixture()
def ednet(tmp_path):
    qpath = tmp_path / "questions.csv"
    qpath.write_text(QUESTIONS)
    zpath = tmp_path / "EdNet-KT1.zip"
    long_rows = [(1_000_000 + 1000 * i, i, ["q1", "q2", "q3", "q4", "q5"][i % 5],
                  "b", 5000) for i in range(6)]
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("KT1/", "")
        # u1: out of order in the file; q9 is unknown and must be dropped
        zf.writestr("KT1/u1.csv", _user_csv([
            (3000, 3, "q3", "c", 3000),
            (1000, 1, "q1", "b", 1000),
            (2000, 2, "q2", "x", 2000),
            (2500, 2, "q9", "a", 2000),
            (4000, 4, "q4", "D", 4000),
        ]))
        zf.writestr("KT1/u2.csv", _user_csv(long_rows))
        zf.writestr("KT1/u3.csv", _user_csv([(1000, 1, "q1", "a", 1000)]))  # too short
        for k in range(4, 8):
            zf.writestr(f"KT1/u{k}.csv", _user_csv(long_rows))
    return zpath, qpath, tmp_path / "out"


def test_load_questions_first_tag_untagged_and_modal_part(ednet):
    _, qpath, _ = ednet
    q = ednet_kt1.load_questions(qpath)
    assert q.loc["q1", "kc_id"] == "tag1"
    assert q.loc["q3", "kc_id"] == "tag7"
    assert q.loc["q4", "kc_id"] == "untagged_p5"
    # tag1 appears in parts 1, 2 and 5 once each -> lowest part on the tie
    assert set(q.loc[["q1", "q2", "q5"], "kc_dimension"]) == {"part1"}
    assert bool(q.loc["q2", "part_differs_from_kc_dimension"])
    assert q.loc["q1", "n_tags"] == 3 and q.loc["q4", "n_tags"] == 0


def test_convert_user_orders_maps_correctness_and_truncates(ednet):
    zpath, qpath, _ = ednet
    q = ednet_kt1.load_questions(qpath)
    with zipfile.ZipFile(zpath) as zf:
        raw = pd.read_csv(zf.open("KT1/u1.csv"), dtype={"question_id": str, "user_answer": str})
    rows, unknown = ednet_kt1.convert_user(raw, "u1", q, max_answers=200)
    assert unknown == 1
    assert rows["item_id"].tolist() == ["q1", "q2", "q3", "q4"]
    assert rows["correct"].tolist() == [1, 0, 1, 1]  # "D" matches "d"
    assert rows["event_index"].tolist() == [0, 1, 2, 3]
    assert rows["timestamp"].tolist() == [1, 2, 3, 4]  # ms -> s
    assert rows["response_time_s"].iloc[0] == 1.0
    assert list(rows.columns) == ednet_kt1.OUTPUT_COLUMNS
    short, _ = ednet_kt1.convert_user(raw, "u1", q, max_answers=2)
    assert short["item_id"].tolist() == ["q1", "q2"]


def test_build_is_nested_deterministic_and_prepare_ready(ednet):
    zpath, qpath, out = ednet
    m1 = ednet_kt1.build(zpath, qpath, out, sizes=[2, 4], seed=1,
                         min_answers=4, max_answers=200, hash_sources=False)
    small = pd.read_csv(out / "kt1_events_2.csv", dtype=str)
    large = pd.read_csv(out / "kt1_events_4.csv", dtype=str)
    assert small["learner_id"].nunique() == 2 and large["learner_id"].nunique() == 4
    assert set(small["learner_id"]) <= set(large["learner_id"])
    assert "u3" not in set(large["learner_id"])  # fewer than min_answers
    m2 = ednet_kt1.build(zpath, qpath, out / "again", sizes=[2, 4], seed=1,
                         min_answers=4, max_answers=200, hash_sources=False)
    again = pd.read_csv(out / "again" / "kt1_events_4.csv", dtype=str)
    pd.testing.assert_frame_equal(large, again)
    assert m1["cohorts"] == m2["cohorts"]
    assert json.loads((out / "ednet_manifest.json").read_text())["nested_cohorts"]

    assert all(c in large.columns for c in REQUIRED_COLUMNS)
    answers, _ = filter_answers(large)
    answers, _ = sort_chronologically(answers)
    question_map, concept_map = build_maps(answers)
    assert len(question_map) == answers["item_id"].nunique()
    assert concept_map["kc_id"].is_unique


def test_fit_bkt_params_recovers_generating_parameters():
    true = BKTParams(p_init=0.3, p_learn=0.2, p_slip=0.1, p_guess=0.2)
    rng = np.random.default_rng(0)
    seqs = []
    for _ in range(400):
        known, seq = rng.random() < true.p_init, []
        for _ in range(15):
            right = rng.random() < (1 - true.p_slip if known else true.p_guess)
            seq.append(int(right))
            known = known or rng.random() < true.p_learn
        seqs.append(seq)
    fitted = fit_bkt_params({"kc": seqs})["kc"]
    assert fitted.p_slip == pytest.approx(true.p_slip, abs=0.11)
    assert fitted.p_guess == pytest.approx(true.p_guess, abs=0.11)
    assert fitted.p_learn == pytest.approx(true.p_learn, abs=0.11)


def test_fit_bkt_params_keeps_fallback_for_thin_kcs():
    fallback = BKTParams()
    assert fit_bkt_params({"thin": [[1, 0, 1]]}, fallback=fallback)["thin"] is fallback


def test_bkt_step_is_what_the_fit_scores():
    # the vectorised fit and bkt_step must agree on a single grid point
    params = BKTParams(p_init=0.5, p_learn=0.1, p_slip=0.1, p_guess=0.2)
    grid = {"p_init": (0.5,), "p_learn": (0.1,), "p_slip": (0.1,), "p_guess": (0.2,)}
    fitted = fit_bkt_params({"k": [[1, 0, 1, 1] * 10]}, grid=grid, min_answers=1)["k"]
    assert fitted == BKTParams(p_init=0.5, p_learn=0.1, p_slip=0.1, p_guess=0.2, p_forget=0.0)
    assert bkt_step(0.5, True, params)[0] == pytest.approx(0.5 * 0.9 + 0.5 * 0.2)
