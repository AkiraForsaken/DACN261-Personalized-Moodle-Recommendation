"""Knowledge levels and quiz history: what the teacher's charts are drawn from."""

import pandas as pd
import pytest

from knowledge_engine.estimate import estimate
from learner_model.builder import build_complete_learner_model
from learner_model.knowledge_levels import (
    KNOWLEDGE_LEVELS,
    LEVEL_COUNT_COLUMNS,
    knowledge_level,
    level_counts,
)
from tests.course_fixture import T0, write_course_export


@pytest.mark.parametrize(
    "attempts, accuracy, level",
    [
        (0, None, "not_assessed"),
        (1, 1.0, "started"),
        (2, 0.0, "started"),
        (3, 0.599, "weak"),
        (3, 0.60, "developing"),
        (5, 0.799, "developing"),
        (5, 0.80, "strong"),
        (8, 1.0, "strong"),
        (4, None, "started"),
    ],
)
def test_level_boundaries(attempts, accuracy, level):
    assert knowledge_level(attempts, accuracy) == level


def test_levels_ignore_the_estimator_and_its_predictions():
    rows = pd.DataFrame(
        {"learner_id": ["1"] * 3, "kc_id": ["a", "b", "c"], "attempt_count": [0, 4, 6],
         "observed_accuracy": [None, 0.5, 0.9]}
    )
    bkt = rows.assign(predicted_correctness=[0.6, 0.55, 0.9], estimator_phase=1)
    akt = rows.assign(predicted_correctness=[0.2, 0.95, 0.1], estimator_phase=3)
    assert level_counts(bkt, ["learner_id"]).equals(level_counts(akt, ["learner_id"]))
    counts = level_counts(bkt, ["learner_id"]).iloc[0]
    assert counts[LEVEL_COUNT_COLUMNS].tolist() == [1, 0, 1, 0, 1]


def _build(tmp_path, as_of=None):
    data = write_course_export(tmp_path / "data")
    # The engine's cutoff is inclusive, the learner model's strict (as in the replay).
    estimate(data, tmp_path / "states", as_of=None if as_of is None else as_of - 1)
    return build_complete_learner_model(
        data_dir=data, output_dir=tmp_path / "model",
        knowledge_state_file=tmp_path / "states" / "learner_kc_state.csv", as_of=as_of,
    )


def test_profiles_carry_levels_that_add_up(tmp_path):
    outputs = _build(tmp_path)
    weak = outputs["weak_areas"].set_index(["learner_id", "entity_id"])
    assert weak.loc[("8", "KC1"), "knowledge_level"] == "weak"  # 1 of 4
    assert weak.loc[("8", "KC2"), "knowledge_level"] == "developing"  # 3 of 4
    assert weak.loc[("9", "KC1"), "knowledge_level"] == "strong"
    assert weak.loc[("7", "KC1"), "knowledge_level"] == "not_assessed"
    assert set(weak["knowledge_level"]) <= set(KNOWLEDGE_LEVELS)
    for name in ("learner_skill_summary", "learner_dimension_summary"):
        frame = outputs[name]
        assert frame[LEVEL_COUNT_COLUMNS].sum(axis=1).eq(frame["kc_count"]).all(), name


def test_quiz_history_lists_every_attempt_and_respects_the_cutoff(tmp_path):
    history = _build(tmp_path)["learner_quiz_attempts"]
    retries = history[history["learner_id"].eq("11")]
    assert retries["attempt"].tolist() == [1, 2]
    assert retries["passed"].tolist() == [0, 1]  # 5 of 8, then 7 of 8, pass mark 70 %
    assert retries["course_module_id"].eq(104).all()
    assert retries["finished_at"].is_monotonic_increasing

    # Cut between learner 11's two attempts: the retry has not happened yet.
    cutoff = T0 + 60 * 1000
    cut = _build(tmp_path / "cut", as_of=cutoff)["learner_quiz_attempts"]
    assert cut[cut["learner_id"].eq("11")]["attempt"].tolist() == [1]
    assert pd.to_datetime(cut["finished_at"]).lt(pd.Timestamp(cutoff, unit="s", tz="UTC")).all()
