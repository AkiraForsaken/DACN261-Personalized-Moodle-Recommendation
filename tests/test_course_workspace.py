"""Course workspaces: one folder per course, and the whole pipeline inside it."""

import json

import pandas as pd
import pytest

from course_workspace import CourseWorkspace, WorkspaceError
from course_workspace import migrate as migration
from tests.course_fixture import COURSE_ID, write_course_export


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MICROLEARNING_HOME", str(tmp_path / "courses"))
    monkeypatch.delenv("MOODLE_COURSE_ID", raising=False)
    monkeypatch.delenv("MICROLEARNING_SITE", raising=False)
    return tmp_path / "courses"


def test_each_course_and_site_gets_its_own_folder(home):
    first = CourseWorkspace.of(10)
    other_course = CourseWorkspace.of(11)
    other_site = CourseWorkspace.of(10, site="school-b")

    assert first.base == home / "local" / "10"
    assert first.data == first.base / "data"
    assert first.active_model_file == first.base / "knowledge" / "active_model.json"
    assert first.kt == first.base / "kt"
    assert first.knowledge_state_file.parent == first.knowledge_states
    assert len({first.base, other_course.base, other_site.base}) == 3
    run = first.refresh_run("20261118T000000Z")
    assert run.data == first.base / "refresh" / "20261118T000000Z" / "data"
    assert run.bundle.parent == run.base


def test_a_course_must_be_named(home, monkeypatch):
    with pytest.raises(WorkspaceError, match="Which course"):
        CourseWorkspace.from_env()
    for bad in ("abc", 1, 0):
        with pytest.raises(WorkspaceError):
            CourseWorkspace.of(bad)
    with pytest.raises(WorkspaceError, match="site name"):
        CourseWorkspace.of(10, site="../x")
    monkeypatch.setenv("MOODLE_COURSE_ID", "12")
    assert CourseWorkspace.from_env().course_id == 12


def test_course_info_is_recorded_and_updated(home):
    workspace = CourseWorkspace.of(10)
    workspace.write_course_info(shortname="ENG-MICRO")
    workspace.write_course_info(fullname="English Microlearning")
    info = workspace.read_course_info()
    assert info["shortname"] == "ENG-MICRO" and info["fullname"] == "English Microlearning"
    assert info["course_id"] == 10 and info["site"] == "local" and info["created_at"]


def test_the_pipeline_reads_and_writes_only_inside_the_course_workspace(home, tmp_path):
    from knowledge_engine.__main__ import main as engine
    from learner_model.builder import main as learner_model
    from moodle_sync.__main__ import main as sync
    from recommender.__main__ import main as recommender

    workspace = CourseWorkspace.of(COURSE_ID)
    write_course_export(workspace.data)
    course = ["--course", str(COURSE_ID)]

    engine(["estimate", *course])
    learner_model(course)
    recommender(["generate", *course])
    sync(["bundle", *course])

    assert workspace.knowledge_state_file.is_file()
    assert (workspace.learner_model / "learner_profile.csv").is_file()
    assert (workspace.recommendations / "recommendations.csv").is_file()
    manifest = json.loads((workspace.bundle / "manifest.json").read_text())
    assert manifest["course_id"] == COURSE_ID
    written = {path for path in tmp_path.rglob("*") if path.is_file()}
    assert all(workspace.base in path.parents for path in written)
    # A second course stays empty: nothing leaks between workspaces.
    assert not CourseWorkspace.of(COURSE_ID + 1).base.exists()


def test_migration_moves_the_old_layout_and_fixes_model_paths(home, tmp_path):
    project = tmp_path / "project"
    data = write_course_export(project / "data")
    old_kt = project / "knowledge_tracing/artifacts/moodle_kt"
    (old_kt / "akt/seed44").mkdir(parents=True)
    (old_kt / "akt/seed44/qid_model.ckpt").write_bytes(b"weights")
    (old_kt / "selected_model.json").write_text(json.dumps(
        {"checkpoint": "knowledge_tracing/artifacts/moodle_kt/akt/seed44/qid_model.ckpt"}
    ))
    (project / "knowledge_engine/artifacts").mkdir(parents=True)
    (project / "knowledge_engine/artifacts/active_model.json").write_text(json.dumps({
        "phase": 3,
        "training_data_dir": str(data),
        "deep_model": {"checkpoint": "knowledge_tracing/artifacts/moodle_kt/akt/seed44/qid_model.ckpt"},
    }))
    (project / "data/processed/moodle_kt").mkdir(parents=True)
    (project / "data/processed/moodle_kt/question_map.csv").write_text("x\n")
    configs = project / "knowledge_tracing/configs"
    configs.mkdir(parents=True)
    (configs / "data_config.json").write_text(json.dumps({
        "english_kt": {"dpath": "data/processed/pykt_english"},
        "moodle_kt": {"dpath": "data/processed/pykt_moodle", "mapping_dir": "data/processed/moodle_kt", "num_q": 32},
    }))

    with pytest.raises(ValueError, match=f"course {COURSE_ID}"):
        migration.plan(CourseWorkspace.of(COURSE_ID + 1), project)
    workspace = CourseWorkspace.of(COURSE_ID)
    moved = migration.apply(workspace, project, log=lambda _: None)

    assert moved and not (project / "data/learner_roster.csv").exists()
    assert (workspace.data / "learner_roster.csv").is_file()
    assert (workspace.kt / "artifacts/akt/seed44/qid_model.ckpt").read_bytes() == b"weights"
    selection = json.loads((workspace.kt / "artifacts/selected_model.json").read_text())
    assert selection["checkpoint"] == "artifacts/akt/seed44/qid_model.ckpt"
    decision = json.loads(workspace.active_model_file.read_text())
    assert decision["deep_model"]["checkpoint"] == "artifacts/akt/seed44/qid_model.ckpt"
    assert decision["training_data_dir"] == str(workspace.data)
    course_config = json.loads((workspace.kt / "data_config.json").read_text())
    assert course_config["moodle_kt"] == {"dpath": "pykt", "mapping_dir": "processed", "num_q": 32}
    assert set(json.loads((configs / "data_config.json").read_text())) == {"english_kt"}
    assert (workspace.kt / "processed/question_map.csv").is_file()
    # Running it twice moves nothing and refuses to overwrite.
    assert migration.plan(workspace, project) == []


def test_the_course_model_is_loaded_from_the_course_workspace(home):
    from knowledge_engine.phase import course_kt_dir

    workspace = CourseWorkspace.of(10)
    assert course_kt_dir(workspace.active_model_file) == workspace.kt.resolve()


def test_fixture_export_is_consistent(tmp_path):
    data = write_course_export(tmp_path / "data", learners=[8])
    assert set(pd.read_csv(data / "learner_roster.csv")["learner_id"]) == {8}
    assert set(pd.read_csv(data / "question_answer_events.csv")["learner_id"]) == {8}
    assert len(pd.read_csv(data / "activity_catalog.csv")) == 9  # structure stays complete
