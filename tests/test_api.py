"""The engine service (api/): auth, events, refresh and full runs, bundles, JSON reads.

Real collector-format data and real model stages (subprocess), a fake collector,
no Moodle: Moodle's side is downloading the zip and importing it.
"""

import io
import json
import sys
import threading
import zipfile

import pandas as pd
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from api.app import create_app  # noqa: E402
from api.jobs import KIND_REFRESH, JobManager, default_run_job  # noqa: E402
from api.settings import Settings  # noqa: E402
from course_workspace import CourseWorkspace  # noqa: E402
from tests.course_fixture import COURSE_ID, write_course_export  # noqa: E402

TOKEN = "t" * 24
HEADERS = {"X-API-Token": TOKEN}


def fake_collect(course_id, site, output_dir, learners):
    write_course_export(output_dir, learners=learners)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MICROLEARNING_HOME", str(tmp_path / "courses"))
    CourseWorkspace.of(COURSE_ID).base.mkdir(parents=True)
    manager = JobManager(run_job=default_run_job(collect=fake_collect, python=sys.executable))
    app = create_app(Settings(api_token=TOKEN, wait_seconds=120), manager)
    return TestClient(app)


def _unzip(content):
    archive = zipfile.ZipFile(io.BytesIO(content))
    return {name: archive.read(name) for name in archive.namelist()}


def test_every_route_but_health_needs_the_token(client):
    assert client.get("/health").json()["courses"] == [COURSE_ID]
    for method, path in [
        ("post", f"/v1/courses/{COURSE_ID}/runs"),
        ("post", f"/v1/courses/{COURSE_ID}/learners/8/refresh"),
        ("get", f"/v1/courses/{COURSE_ID}/cohort"),
        ("get", "/v1/jobs/abc"),
    ]:
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path, headers={"X-API-Token": "wrong" * 5}).status_code == 401
    assert client.get("/docs").status_code == 404


def test_short_tokens_are_refused(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "short")
    with pytest.raises(RuntimeError, match="at least"):
        Settings.from_env()


def test_full_run_then_refresh_give_bundles_moodle_can_import(client):
    run = client.post(f"/v1/courses/{COURSE_ID}/runs?wait=1", headers=HEADERS)
    assert run.status_code == 200, run.text
    snapshot = run.json()["result"]["bundle"]
    assert snapshot["kind"] == "snapshot" and snapshot["rows"]["profiles.csv"] == 5

    files = _unzip(client.get(f"/v1/courses/{COURSE_ID}/bundles/{snapshot['bundle_id']}.zip",
                              headers=HEADERS).content)
    manifest = json.loads(files["manifest.json"])
    assert manifest["kind"] == "snapshot" and set(manifest["files"]) <= set(files)

    refresh = client.post(f"/v1/courses/{COURSE_ID}/learners/8/refresh?wait=1", headers=HEADERS)
    assert refresh.status_code == 200, refresh.text
    update = refresh.json()["result"]["bundle"]
    assert update["kind"] == "learner_update" and update["learners"] == [8]
    files = _unzip(client.get(f"/v1/courses/{COURSE_ID}/bundles/{update['bundle_id']}.zip",
                              headers=HEADERS).content)
    assert set(pd.read_csv(io.BytesIO(files["profiles.csv"]))["userid"]) == {8}

    # The learner's JSON comes from the refresh, the cohort from the full run.
    profile = client.get(f"/v1/courses/{COURSE_ID}/learners/8/profile", headers=HEADERS).json()
    assert profile["source"] == "refresh" and profile["kcs"]
    recs = client.get(f"/v1/courses/{COURSE_ID}/learners/8/recommendations", headers=HEADERS).json()
    assert recs["queue"][0]["rank"] == 1 and all(r["trigger_cmid"] for r in recs["failure_branches"])
    assert client.get(f"/v1/courses/{COURSE_ID}/cohort", headers=HEADERS).json()["learners"] == 5


def test_a_replaced_or_unknown_bundle_is_gone(client):
    response = client.get(f"/v1/courses/{COURSE_ID}/bundles/{'0' * 20}.zip", headers=HEADERS)
    assert response.status_code == 410
    assert client.get(f"/v1/courses/{COURSE_ID}/bundles/x.zip", headers=HEADERS).status_code == 422


def test_unknown_course_and_event_type_are_refused(client):
    assert client.post("/v1/courses/999/runs", headers=HEADERS).status_code == 404
    bad = client.post(f"/v1/courses/{COURSE_ID}/events", headers=HEADERS,
                      json={"userid": 8, "type": "clicked"})
    assert bad.status_code == 422


def test_events_for_several_learners_join_one_queued_refresh(tmp_path, monkeypatch):
    """While a job runs, new requests wait in one batched job (the worker's behaviour)."""
    monkeypatch.setenv("MICROLEARNING_HOME", str(tmp_path / "courses"))
    CourseWorkspace.of(COURSE_ID).base.mkdir(parents=True)
    release, started, runs = threading.Event(), threading.Event(), []

    def run_job(workspace, job):
        runs.append(sorted(job.learners))
        started.set()
        release.wait(10)
        return {"learners": sorted(job.learners)}

    manager = JobManager(run_job=run_job)
    client = TestClient(create_app(Settings(api_token=TOKEN), manager))
    post = lambda user, kind: client.post(f"/v1/courses/{COURSE_ID}/events", headers=HEADERS,
                                          json={"userid": user, "type": kind}).json()
    first = post(7, "new_learner")
    assert started.wait(10)  # the first job is running
    second, third = post(8, "list_done"), post(11, "quiz_failed")
    assert second["job_id"] == third["job_id"] != first["job_id"]
    assert third["learners"] == [8, 11]
    release.set()
    job = manager.job(third["job_id"])
    assert job.finished.wait(10) and job.state == "done"
    assert runs == [[7], [8, 11]]
    assert job.reasons == {8: "list_done", 11: "quiz_failed"} and job.kind == KIND_REFRESH


def test_a_failing_job_is_reported_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setenv("MICROLEARNING_HOME", str(tmp_path / "courses"))
    CourseWorkspace.of(COURSE_ID).base.mkdir(parents=True)

    def run_job(workspace, job):
        raise RuntimeError("collector cannot reach the database")

    client = TestClient(create_app(Settings(api_token=TOKEN), JobManager(run_job=run_job)))
    response = client.post(f"/v1/courses/{COURSE_ID}/learners/8/refresh?wait=1", headers=HEADERS)
    assert response.status_code == 502
    assert "cannot reach the database" in response.json()["detail"]["error"]


def test_dashboard_routes_need_the_token(client):
    for method, path in [
        ("get", "/v1/jobs"),
        ("get", f"/v1/courses/{COURSE_ID}/status"),
        ("post", f"/v1/courses/{COURSE_ID}/train"),
    ]:
        assert getattr(client, method)(path).status_code == 401


def test_status_reports_readiness_and_the_recorded_decision(client):
    write_course_export(CourseWorkspace.of(COURSE_ID).data)
    status = client.get(f"/v1/courses/{COURSE_ID}/status", headers=HEADERS).json()
    assert status["course_id"] == COURSE_ID
    assert status["training_population"] in status["readiness_by_population"]
    assert status["active_model"]["phase"] == 1  # nothing trained yet
    assert status["active_model"]["gate"] is None
    assert status["pending_refresh"] == []


def test_status_without_collected_data_is_unavailable(client):
    assert client.get(f"/v1/courses/{COURSE_ID}/status", headers=HEADERS).status_code == 503


def test_training_is_refused_when_the_course_is_not_ready(client):
    write_course_export(CourseWorkspace.of(COURSE_ID).data)
    response = client.post(f"/v1/courses/{COURSE_ID}/train", headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"]["unmet"]


def test_training_runs_as_a_job_and_is_listed(tmp_path, monkeypatch):
    """A ready course trains in its own job; the job list shows it, newest first."""
    monkeypatch.setenv("MICROLEARNING_HOME", str(tmp_path / "courses"))
    CourseWorkspace.of(COURSE_ID).base.mkdir(parents=True)
    import api.app as app_module

    ready = {"ready": True, "unmet": []}
    monkeypatch.setattr(app_module, "_course_status", lambda space: {
        "training_population": "simulated", "readiness_by_population": {"simulated": ready}})
    seen = []

    def run_job(workspace, job):
        seen.append((job.kind, job.options))
        return {"phase": 2}

    client = TestClient(create_app(Settings(api_token=TOKEN), JobManager(run_job=run_job)))
    job = client.post(f"/v1/courses/{COURSE_ID}/train?wait=1", headers=HEADERS).json()
    assert job["state"] == "done" and job["kind"] == "train"
    assert seen == [("train", {"population": "simulated"})]
    assert client.post(f"/v1/courses/{COURSE_ID}/train?population=moodle_collector",
                       headers=HEADERS).status_code == 422
    listed = client.get(f"/v1/jobs?course_id={COURSE_ID}", headers=HEADERS).json()
    assert [entry["job_id"] for entry in listed["jobs"]] == [job["job_id"]]


def test_status_names_the_training_population_not_the_data_mix(client):
    """A mixed course trained on its simulated learners reports 'simulated'."""
    space = CourseWorkspace.of(COURSE_ID)
    write_course_export(space.data)
    space.active_model_file.parent.mkdir(parents=True, exist_ok=True)
    space.active_model_file.write_text(json.dumps({
        "phase": 2, "estimator": "bkt", "cohort": "mixed", "training_population": "simulated",
        "gate": {"deep": {"auc": 0.6}, "bkt": {"auc": 0.6}, "n_targets": 10}}))
    status = client.get(f"/v1/courses/{COURSE_ID}/status", headers=HEADERS).json()
    assert status["active_model"]["population"] == "simulated"
