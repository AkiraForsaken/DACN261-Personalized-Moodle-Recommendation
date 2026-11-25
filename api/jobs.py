"""Background jobs: one runner per course, one job at a time, refresh requests batched.

A refresh request for a learner joins the course's queued refresh job if
there is one (several learners are recomputed in one run, as the worker did);
a full run joins a queued full run. Jobs run under the course's file lock
(``moodle_sync.worker.course_lock``), so the API and a running worker never
write the same workspace at once.
"""

import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone

from course_workspace import CourseWorkspace
from course_workspace import PROJECT_ROOT
from moodle_sync.refresh import full_run, model_python, refresh_learners
from moodle_sync.worker import WorkerBusy, course_lock

from .bundles import PublishTransport

KIND_REFRESH = "refresh"
KIND_FULL = "full_run"
# Training is deliberate: only an administrator's explicit request (the dashboard's
# "Train course model" or the CLI) creates this job; refreshes and full runs never train.
KIND_TRAIN = "train"
QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
KEEP_JOBS = 500
LOCK_RETRY_SECONDS = 2.0
LOCK_TIMEOUT_SECONDS = 900.0


def _now():
    return datetime.now(timezone.utc).isoformat()


class Job:
    def __init__(self, course_id, kind, learners=()):
        self.id = uuid.uuid4().hex[:16]
        self.course_id, self.kind = int(course_id), kind
        self.learners = set(int(learner) for learner in learners)
        self.reasons = {}
        self.options = {}
        self.state, self.result, self.error = QUEUED, None, None
        self.created_at, self.started_at, self.finished_at = _now(), None, None
        self.finished = threading.Event()

    def describe(self):
        return {
            "job_id": self.id,
            "course_id": self.course_id,
            "kind": self.kind,
            "learners": sorted(self.learners) if self.kind == KIND_REFRESH else None,
            "reasons": self.reasons or None,
            "options": self.options or None,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class CourseRunner:
    """The queue and worker thread of one course."""

    def __init__(self, workspace, run_job):
        self.workspace, self._run_job = workspace, run_job
        self._queue, self._lock = deque(), threading.Lock()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"course-{workspace.course_id}", daemon=True
        )
        self._thread.start()

    def submit(self, kind, learners=(), reason=None, options=None):
        """Queue work; join a queued job of the same kind. Returns the job."""
        with self._lock:
            for job in self._queue:
                if job.kind == kind and job.state == QUEUED and job.options == (options or {}):
                    break
            else:
                job = Job(self.workspace.course_id, kind)
                job.options = dict(options or {})
                self._queue.append(job)
            job.learners |= {int(learner) for learner in learners}
            if reason:
                for learner in learners:
                    job.reasons[int(learner)] = reason
        self._wake.set()
        return job

    def pending_learners(self):
        with self._lock:
            return sorted({l for job in self._queue for l in job.learners})

    def _loop(self):
        while True:
            self._wake.wait()
            while True:
                with self._lock:
                    if not self._queue:
                        self._wake.clear()
                        break
                    job = self._queue[0]
                    job.state, job.started_at = RUNNING, _now()
                try:
                    job.result = self._locked(job)
                    job.state = DONE
                except Exception as error:  # reported to the client, never kills the thread
                    job.state, job.error = FAILED, f"{type(error).__name__}: {error}"
                finally:
                    job.finished_at = _now()
                    with self._lock:
                        self._queue.popleft()
                    job.finished.set()

    def _locked(self, job):
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                with course_lock(self.workspace):
                    return self._run_job(self.workspace, job)
            except WorkerBusy:
                if time.monotonic() > deadline:
                    raise
                time.sleep(LOCK_RETRY_SECONDS)


def train_course(workspace, population=None, python=None):
    """Train the course's deep model and apply the promotion gate (knowledge_engine train).

    Runs in the PyTorch environment like the model stages; returns the new
    active-model record's decision. Never forced: a course that is not ready fails.
    """
    command = [model_python(python), "-m", "knowledge_engine", "train",
               "--course", str(workspace.course_id), "--site", workspace.site]
    if population:
        command += ["--population", population]
    result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"training failed (exit {result.returncode}):\n{result.stderr.strip()[-2000:]}")
    from knowledge_engine.phase import load_active_model

    model = load_active_model(workspace.active_model_file) or {}
    gate = model.get("gate") or {}
    return {
        "phase": model.get("phase"),
        "estimator": model.get("estimator"),
        "population": model.get("training_population") or model.get("cohort"),
        "deep_auc": (gate.get("deep") or {}).get("auc"),
        "bkt_auc": (gate.get("bkt") or {}).get("auc"),
        "n_targets": gate.get("n_targets"),
    }


def default_run_job(collect=None, python=None):
    """How a job is executed: the refresh/full-run code, publishing instead of pushing."""

    def run(workspace, job):
        if job.kind == KIND_TRAIN:
            return train_course(workspace, job.options.get("population"), python=python)
        transport = PublishTransport()
        if job.kind == KIND_REFRESH:
            summary = refresh_learners(workspace, job.learners, transport, python=python, collect=collect)
        else:
            summary = full_run(workspace, transport, python=python, collect=collect)
        bundle = summary.pop("import")
        return {**summary, "bundle": bundle}

    return run


class JobManager:
    """Every course's runner and the recent jobs, by id."""

    def __init__(self, site=None, run_job=None):
        self.site = site
        self._run_job = run_job or default_run_job()
        self._runners, self._jobs = {}, OrderedDict()
        self._lock = threading.Lock()

    def workspace(self, course_id):
        return CourseWorkspace.of(course_id, self.site)

    def runner(self, course_id):
        with self._lock:
            if course_id not in self._runners:
                self._runners[course_id] = CourseRunner(self.workspace(course_id), self._run_job)
            return self._runners[course_id]

    def submit(self, course_id, kind, learners=(), reason=None, options=None):
        job = self.runner(int(course_id)).submit(kind, learners, reason, options)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > KEEP_JOBS:
                self._jobs.popitem(last=False)
        return job

    def job(self, job_id):
        return self._jobs.get(job_id)

    def recent(self, course_id=None, limit=50):
        """The most recent jobs, newest first, optionally of one course."""
        jobs = [job for job in reversed(list(self._jobs.values()))
                if course_id is None or job.course_id == int(course_id)]
        return [job.describe() for job in jobs[:limit]]

    def active(self):
        return [job.describe() for job in list(self._jobs.values()) if job.state in (QUEUED, RUNNING)]
