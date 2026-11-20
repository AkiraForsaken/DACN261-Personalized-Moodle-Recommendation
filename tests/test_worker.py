"""The refresh worker: polling Moodle's requests, refreshing only those learners."""

import json
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytest

from course_workspace import CourseWorkspace
from moodle_sync.refresh import full_run, refresh_learners
from moodle_sync.worker import (
    MAX_BACKOFF_SECONDS,
    Worker,
    WorkerBusy,
    course_lock,
    next_full_run,
    pending_requests,
)
from tests.course_fixture import COURSE_ID, write_course_export


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MICROLEARNING_HOME", str(tmp_path / "courses"))
    return CourseWorkspace.of(COURSE_ID)


class FakeCursor:
    def __init__(self, rows):
        self.rows, self.query, self.params = rows, None, None

    def execute(self, query, params=()):
        self.query, self.params = query, params

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)

    def close(self):
        pass


class FakeTransport:
    name = "fake"

    def __init__(self):
        self.pushed = []

    def push(self, bundle_dir, course_id):
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        self.pushed.append((manifest["kind"], manifest["learners"], course_id))
        return {"kind": manifest["kind"], "updated": manifest["learners"]}


def fake_collect(course_id, site, output_dir, learners):
    write_course_export(output_dir, learners=learners)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


def test_pending_requests_are_grouped_per_learner():
    cursor = FakeCursor([(8, "list_done"), (11, "quiz_failed"), (8, "quiz_failed")])
    assert pending_requests(cursor, 42) == {8: ["list_done", "quiz_failed"], 11: ["quiz_failed"]}
    assert "timeprocessed IS NULL" in cursor.query and cursor.params == (42,)
    assert "m_local_microlearning_refresh" in cursor.query


def test_a_refresh_recomputes_and_publishes_only_the_requested_learners(workspace):
    """Real collector-format data, real model stages (subprocess), fake Moodle."""
    write_course_export(workspace.data)  # a full run happened before
    transport = FakeTransport()

    summary = refresh_learners(workspace, [11, 8], transport, python=sys.executable,
                               collect=fake_collect, log=lambda _: None)

    assert transport.pushed == [("learner_update", [8, 11], COURSE_ID)]
    assert summary["learners"] == [8, 11] and summary["seconds"]["total"] > 0
    bundle = workspace.refresh / summary["run_dir"].rsplit("/", 1)[-1] / "bundle"
    assert set(pd.read_csv(bundle / "profiles.csv")["userid"]) == {8, 11}
    # The course's own outputs were not touched by the refresh.
    assert not workspace.recommendations.exists()


def test_full_run_publishes_a_snapshot(workspace):
    transport = FakeTransport()
    result = full_run(workspace, transport, python=sys.executable, collect=fake_collect,
                      log=lambda _: None)
    assert transport.pushed == [("snapshot", None, COURSE_ID)]
    assert result["rows"]["profiles.csv"] == 5


def _worker(workspace, rows, **kwargs):
    calls = {"refresh": [], "full": 0}

    def refresh(ws, learners, transport, python, log):
        calls["refresh"].append(learners)
        if kwargs.get("refuse"):
            raise RuntimeError(kwargs["refuse"])
        return {"learners": learners}

    def full(ws, transport, python, log):
        calls["full"] += 1
        return {"kind": "snapshot"}

    clock = kwargs.get("clock") or Clock(datetime(2026, 11, 18, 12, 0).astimezone())
    worker = Worker(workspace, FakeTransport(), lambda: FakeConnection(rows), poll_seconds=10,
                    full_at=kwargs.get("full_at", "03:00"), clock=clock, sleep=lambda _: None,
                    refresh=refresh, full=full, log=lambda _: None)
    return worker, calls, clock


def test_worker_refreshes_pending_learners_and_idles_otherwise(workspace):
    worker, calls, _ = _worker(workspace, [(8, "list_done"), (7, "new_learner")])
    assert worker.tick() == "refresh"
    assert calls["refresh"] == [[7, 8]]
    state = json.loads((workspace.worker / "state.json").read_text())
    assert state["last_refresh"] == {"learners": [7, 8]}

    idle, calls, _ = _worker(workspace, [])
    assert idle.tick() == "idle" and calls["refresh"] == []


def test_worker_runs_the_daily_full_rebuild_once_per_day(workspace):
    clock = Clock(datetime(2026, 11, 18, 2, 59).astimezone())
    worker, calls, _ = _worker(workspace, [], clock=clock)
    assert worker.tick() == "idle" and calls["full"] == 0
    clock.now += timedelta(minutes=2)
    assert worker.tick() == "full" and calls["full"] == 1
    assert worker.tick() == "idle" and calls["full"] == 1
    assert worker.next_full.date() == (clock.now + timedelta(days=1)).date()
    assert next_full_run(clock.now, "") is None


def test_a_refused_learner_update_falls_back_to_a_full_rebuild(workspace):
    worker, calls, _ = _worker(workspace, [(8, "list_done")],
                               refuse="Import failed: errormappingchanged")
    assert worker.tick() == "full"
    assert calls["refresh"] == [[8]] and calls["full"] == 1


def test_failures_back_off_and_recover(workspace):
    worker, calls, _ = _worker(workspace, [(8, "list_done")], refuse="docker cp failed")
    assert [worker.tick() for _ in range(3)] == ["error"] * 3
    assert worker.pause_seconds() == 10 * 2 ** 3
    worker.failures = 30
    assert worker.pause_seconds() == MAX_BACKOFF_SECONDS
    state = json.loads((workspace.worker / "state.json").read_text())
    assert state["last_error"]["consecutive_failures"] == 3


def test_only_one_worker_per_course(workspace):
    with course_lock(workspace):
        with pytest.raises(WorkerBusy):
            with course_lock(workspace):
                pass
    with course_lock(workspace):  # released again
        pass


def test_worker_run_once_holds_the_lock_and_returns(workspace):
    worker, calls, _ = _worker(workspace, [(9, "quiz_failed")])
    assert worker.run(once=True) == "refresh"
    assert calls["refresh"] == [[9]]
