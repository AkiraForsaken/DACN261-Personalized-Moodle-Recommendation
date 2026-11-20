"""Offline replay metrics and the Moodle import bundle."""

import json

import pandas as pd
import pytest

from moodle_sync.bundle import build_bundle
from recommender import schemas
from recommender.evaluate import (
    agreement_metrics,
    choose_cutoffs,
    coverage_metrics,
    next_activities,
    stability,
    summarize,
    targeting_metrics,
)
from recommender.generate import generate
from tests.test_recommender import LESSON_A, _write_snapshot


def _recs(rows):
    frame = pd.DataFrame(
        rows, columns=["learner_id", "rank", "course_module_id", "kc_id", "reason_code"]
    )
    frame["basis"] = frame["reason_code"].map(schemas.REASON_CODES)
    return frame


LOG = pd.DataFrame(
    {
        "learner_id": ["a", "a", "a", "b", "b"],
        "course_module_id": [1, 2, 2, 9, 3],
        "time": [50, 110, 120, 105, 400],
    }
)


def test_cutoffs_and_next_activities_respect_time():
    assert choose_cutoffs(LOG, 1) == [110]
    assert choose_cutoffs(LOG.iloc[0:0], 3) == []
    # At or after the cutoff, inside the horizon, first occurrence order.
    assert next_activities(LOG, 100, 50) == {"a": [2], "b": [9]}
    assert next_activities(LOG, 100, 1000) == {"a": [2], "b": [9, 3]}


def test_agreement_counts_only_learners_active_in_the_horizon():
    recs = _recs(
        [
            ("a", 1, 7, None, schemas.CONTINUE_COURSE),
            ("a", 2, 2, None, schemas.CONTINUE_COURSE),
            ("b", 1, 3, None, schemas.CONTINUE_COURSE),
            ("c", 1, 1, None, schemas.CONTINUE_COURSE),  # inactive: not evaluated
        ]
    )
    result = agreement_metrics(recs, {"a": [2], "b": [9, 3]})

    assert result["active_learners"] == 2
    assert result["hit"] == 2
    assert result["reciprocal_rank_sum"] == pytest.approx(0.5 + 1.0)
    assert result["first_next_hit"] == 1  # a's first next activity (2) is in a's list


def test_targeting_compares_flagged_kcs_with_the_learners_other_known_kcs():
    answers = pd.DataFrame(
        {
            "learner_id": ["a"] * 7,
            "kc_id": ["K1", "K2", "K1", "K1", "K2", "K2", "K3"],
            "time": [10, 20, 200, 210, 220, 230, 240],
            "correct": [0, 1, 0, 1, 1, 1, 0],
        }
    )
    recs = _recs(
        [
            ("a", 1, 5, "K1", schemas.REVIEW_WEAK_KC),
            ("a", 2, 6, "K3", schemas.PREPARE_PREDICTED_DIFFICULTY),
        ]
    )
    result = targeting_metrics(recs, answers, cutoff=100)

    assert (result["targeted_answers"], result["targeted_correct"]) == (2, 1)  # K1 later
    assert (result["other_answers"], result["other_correct"]) == (2, 2)  # K2 later
    assert (result["predicted_answers"], result["predicted_correct"]) == (1, 0)  # K3
    assert result["unseen_answers"] == 0


def test_coverage_stability_and_summary():
    first = _recs(
        [
            ("a", 1, 1, "K1", schemas.REVIEW_WEAK_KC),
            ("a", 2, 2, None, schemas.CONTINUE_COURSE),
            ("b", 1, 3, None, schemas.CONTINUE_COURSE),
        ]
    )
    second = first.assign(course_module_id=[1, 4, 3])

    coverage = coverage_metrics(first, ["a", "b", "c"])
    assert coverage["learners_with_list"] == 2
    assert coverage["learners_with_personalised_item"] == 1
    assert coverage["rows_evidence"] == 1 and coverage["rows_course_order"] == 2
    assert stability(None, first) == (None, 0)
    overlap, compared = stability(first, second)
    assert compared == 2 and overlap == pytest.approx((1 / 3 + 1.0) / 2)

    row = {
        "estimator": "bkt", "cutoff": 100, "policy": "rules", **coverage,
        **agreement_metrics(first, {"a": [2]}),
        **targeting_metrics(first, pd.DataFrame(columns=["learner_id", "kc_id", "time", "correct"]), 100),
        "stability_jaccard": None, "stability_learners": 0,
    }
    summary = summarize(pd.DataFrame([row])).iloc[0]
    assert summary["hit_at_n"] == 1.0 and summary["mrr"] == 0.5
    assert summary["share_rows_course_order"] == pytest.approx(0.6667)
    assert summary["targeted_kc_later_accuracy"] is None


def _complete_build(profile_dir, data_dir):
    """Add the remaining learner-model files a bundle needs to the small snapshot."""
    profiles = pd.read_csv(profile_dir / "learner_profile.csv", dtype=str).assign(
        knowledge_model=["bkt_item_level", "akt"],
        knowledge_status=["cold_start_only", "available"],
        engagement_status=["no_observations", "available"],
    )
    profiles.to_csv(profile_dir / "learner_profile.csv", index=False)
    states = pd.read_csv(profile_dir / "learner_kc_state.csv", dtype=str).assign(
        kc_dimension="grammar", state_status="estimated"
    )
    states.to_csv(profile_dir / "learner_kc_state.csv", index=False)
    weak = pd.read_csv(profile_dir / "weak_areas.csv", dtype=str).assign(reason="because")
    weak.to_csv(profile_dir / "weak_areas.csv", index=False)
    pd.DataFrame(
        {"kc_id": ["KC-A", "KC-B"], "topic": ["Unit 1", "Unit 1"]}
    ).to_csv(profile_dir / "kc_skill_map.csv", index=False)
    pd.DataFrame(
        [dict(learner_id="8", active_session_count=3, estimated_learning_time_seconds=600,
              completed_activity_count=1, course_progress=0.125,
              last_active_at="2026-06-01T00:00:00+00:00")]
    ).to_csv(profile_dir / "learner_engagement.csv", index=False)
    pd.DataFrame(
        [dict(learner_id="8", total_quiz_attempts=2, historical_learning_days=2)]
    ).to_csv(profile_dir / "learner_history.csv", index=False)
    pd.DataFrame(
        [dict(learner_id="8", skill_id="grammar|Unit 1", skill_name="Grammar: Unit 1",
              kc_dimension="grammar", kc_count=2, estimated_kc_count=2,
              mean_predicted_correctness=0.55, mean_observed_accuracy=0.5,
              total_attempt_count=16)]
    ).to_csv(profile_dir / "learner_skill_summary.csv", index=False)
    manifest = json.loads((profile_dir / "build_manifest.json").read_text())
    manifest.update(
        generated_at="2026-11-16T00:00:00+00:00",
        source_population="mixed",
        population_counts={"moodle_collector": 1, "simulated": 1},
        profile_schema_version="2.0.0",
    )
    (profile_dir / "build_manifest.json").write_text(json.dumps(manifest))
    pd.DataFrame(
        {"learner_id": ["7", "8"], "course_id": 10, "is_simulated": [0, 1]}
    ).to_csv(data_dir / "learner_roster.csv", index=False)


def test_bundle_is_the_flat_contract_the_moodle_plugin_imports(tmp_path):
    profile_dir, data_dir = _write_snapshot(tmp_path)
    _complete_build(profile_dir, data_dir)
    generate(profile_dir, data_dir, tmp_path / "recs")

    manifest = build_bundle(profile_dir, tmp_path / "recs", data_dir, tmp_path / "bundle")

    assert manifest["course_id"] == 10 and manifest["cohort"] == "mixed"
    assert manifest["bundle_schema_version"] == 3 and manifest["kind"] == "snapshot"
    queue = pd.read_csv(tmp_path / "recs" / "recommendations.csv")
    assert {name: f["rows"] for name, f in manifest["files"].items()} == {
        "profiles.csv": 2, "kc_states.csv": 2, "skills.csv": 1, "dimensions.csv": 0,
        "attempts.csv": 0,
        "recommendations.csv": len(queue),  # the full queue, not only the top 5
    }
    profiles = pd.read_csv(tmp_path / "bundle" / "profiles.csv").set_index("userid")
    assert profiles.loc[7, "population"] == "moodle_collector"
    assert profiles.loc[8, "last_active"] == 1780272000  # ISO time as Unix seconds
    assert pd.isna(profiles.loc[7, "session_count"])  # no activity yet, not zero
    recs = pd.read_csv(tmp_path / "bundle" / "recommendations.csv")
    assert recs.iloc[0][["userid", "rank", "cmid"]].tolist() == [7, 1, 101]
    assert LESSON_A in recs.loc[recs["userid"].eq(8), "cmid"].tolist()
    states = pd.read_csv(tmp_path / "bundle" / "kc_states.csv")
    assert states["topic"].eq("Unit 1").all() and states["reason"].eq("because").all()


def test_bundle_refuses_mismatched_or_non_moodle_inputs(tmp_path):
    profile_dir, data_dir = _write_snapshot(tmp_path)
    _complete_build(profile_dir, data_dir)
    generate(profile_dir, data_dir, tmp_path / "recs")

    # Profiles rebuilt after the recommendations were generated.
    manifest_path = profile_dir / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps({**manifest, "generated_at": "later"}))
    with pytest.raises(ValueError, match="another learner-model build"):
        build_bundle(profile_dir, tmp_path / "recs", data_dir, tmp_path / "bundle")

    # Offline-simulator learner ids are not Moodle user ids.
    generate(profile_dir, data_dir, tmp_path / "recs")
    profiles = pd.read_csv(profile_dir / "learner_profile.csv", dtype=str)
    profiles.loc[0, "learner_id"] = "sim-0001"
    profiles.to_csv(profile_dir / "learner_profile.csv", index=False)
    with pytest.raises(ValueError, match="not Moodle user ids"):
        build_bundle(profile_dir, tmp_path / "recs", data_dir, tmp_path / "bundle")
