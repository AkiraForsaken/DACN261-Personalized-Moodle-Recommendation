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
