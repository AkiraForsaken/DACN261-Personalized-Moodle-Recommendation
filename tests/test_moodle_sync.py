"""Bundle v2 and the per-learner refresh: what Moodle imports, and why a refresh is safe."""

import json

import pandas as pd
import pytest

from course_workspace import CourseWorkspace
from moodle_sync.bundle import KIND_LEARNER_UPDATE, build_bundle
from moodle_sync.pipeline import run_model_stages
from tests.course_fixture import COURSE_ID, HISTORIES, T0, write_course_export

GENERATED = "2026-11-18T00:00:00+00:00"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MICROLEARNING_HOME", str(tmp_path / "courses"))
    return CourseWorkspace.of(COURSE_ID)


def _full_run(workspace):
    write_course_export(workspace.data)
    return run_model_stages(workspace, workspace, generated_at=GENERATED)


def _refresh(workspace, learners):
    run = workspace.refresh_run("test")
    write_course_export(run.data, learners=learners)
    return run, run_model_stages(run, workspace, KIND_LEARNER_UPDATE, generated_at=GENERATED)


def _bundle(layout, name):
    return pd.read_csv(layout.bundle / name)


def test_snapshot_bundle_holds_the_new_files_and_settings(workspace):
    manifest = _full_run(workspace)

    assert manifest["bundle_schema_version"] == 3 and manifest["kind"] == "snapshot"
    assert manifest["learners"] is None
    assert manifest["collected_at"] == T0 + 86_400 * 10  # from collection_manifest.json
    assert (manifest["top_n"], manifest["max_remediation"], manifest["max_prepare"]) == (5, 3, 2)
    assert set(manifest["files"]) == {
        "profiles.csv", "kc_states.csv", "skills.csv", "dimensions.csv", "attempts.csv",
        "recommendations.csv",
    }
    for name, entry in manifest["files"].items():
        assert entry["rows"] == len(_bundle(workspace, name)), name

    states = _bundle(workspace, "kc_states.csv").set_index(["userid", "kc_id"])
    kc1 = states.loc[(8, "KC1")]
    assert (kc1["knowledge_level"], kc1["attempt_count"], kc1["correct_count"]) == ("weak", 4, 1)
    assert (kc1["lesson_cmid"], kc1["quiz_cmid"], kc1["unit_number"], kc1["kc_order"]) == (102, 104, 1, 1)
    assert states.loc[(8, "KC3"), "unit_number"] == 2
    assert states.loc[(7, "KC1"), "knowledge_level"] == "not_assessed"

    dimensions = _bundle(workspace, "dimensions.csv").set_index(["userid", "dimension"])
    assert dimensions.loc[(10, "grammar"), "strong_kc_count"] == 1  # KC1 4/4; KC3 3/4 = developing
    assert dimensions.loc[(10, "grammar"), "developing_kc_count"] == 1
    skills = _bundle(workspace, "skills.csv")
    assert skills["unit_number"].notna().all()

    attempts = _bundle(workspace, "attempts.csv")
    retries = attempts[attempts["userid"].eq(11)]
    assert retries[["cmid", "attempt", "passed"]].values.tolist() == [[104, 1, 0], [104, 2, 1]]

    recs = _bundle(workspace, "recommendations.csv")
    assert set(recs["group"]) <= {"remediation", "prepare", "forward", "contingency"}
    branch = recs["group"].eq("contingency")
    assert recs.loc[branch, "trigger_cmid"].notna().all() and recs.loc[~branch, "trigger_cmid"].isna().all()
    new_learner = recs[recs["userid"].eq(7) & ~branch]
    assert len(new_learner) == 8  # every recommendable activity of the course


def test_learner_update_bundle_names_its_learners(workspace):
    _full_run(workspace)
    run, manifest = _refresh(workspace, [8])
    assert manifest["kind"] == KIND_LEARNER_UPDATE and manifest["learners"] == [8]
    for name in ("profiles.csv", "kc_states.csv", "recommendations.csv", "attempts.csv"):
        assert set(_bundle(run, name)["userid"]) == {8}, name


@pytest.mark.parametrize("learner", sorted(HISTORIES))
def test_a_refresh_gives_the_same_rows_as_a_full_run(workspace, learner):
    """The safety argument of per-learner refresh: no stage depends on other learners."""
    _full_run(workspace)
    run, _ = _refresh(workspace, [learner])
    for name in ("profiles.csv", "kc_states.csv", "skills.csv", "dimensions.csv",
                 "attempts.csv", "recommendations.csv"):
        full = _bundle(workspace, name)
        full = full[full["userid"].eq(learner)].reset_index(drop=True)
        mine = _bundle(run, name).reset_index(drop=True)
        pd.testing.assert_frame_equal(full, mine, check_dtype=False, obj=name)


def test_bundle_refuses_an_unknown_kind(workspace):
    _full_run(workspace)
    with pytest.raises(ValueError, match="kind"):
        build_bundle(workspace.learner_model, workspace.recommendations, workspace.data,
                     workspace.bundle, kind="partial")
    json.loads((workspace.bundle / "manifest.json").read_text())  # earlier bundle intact
