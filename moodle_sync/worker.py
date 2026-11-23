"""The refresh worker: answers Moodle's per-learner refresh requests.

    python moodle-sync.py worker --course <id> [--poll 15] [--full-at 03:00] [--once]

Moodle (local_microlearning's observer) writes a row into
``local_microlearning_refresh`` when a learner has worked through their list,
failed a quiz, or was newly enrolled. The worker

  1. polls that table (one read-only query per tick) for pending requests of
     its course;
  2. refreshes exactly those learners (``refresh.refresh_learners``); the
     import marks their requests processed;
  3. once a day at ``--full-at`` rebuilds the whole course (a new snapshot),
     which also answers every pending request;
  4. falls back to a full run when Moodle refuses a learner update because
     the course changed underneath it (new question mapping, no snapshot).

Only one worker per course runs at a time (a lock file in the course's
``worker/`` folder). A failed refresh leaves the requests pending; the
worker retries with a growing pause. The block keeps working meanwhile,
from the learner's queue.
"""

import fcntl
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .refresh import full_run, refresh_learners

FULL_RUN_ERRORS = ("errormappingchanged", "errornosnapshotforupdate", "mapping", "needs an active snapshot")
MAX_BACKOFF_SECONDS = 15 * 60


class WorkerBusy(RuntimeError):
    """Another worker holds this course's lock."""


def pending_requests(cursor, course_id, table_prefix="m_"):
    """``{userid: [reasons]}`` of the course's unprocessed refresh requests."""
    cursor.execute(
        f"SELECT userid, reason FROM {table_prefix}local_microlearning_refresh "
        "WHERE courseid = ? AND timeprocessed IS NULL ORDER BY timecreated, id",
        (int(course_id),),
    )
    pending = {}
    for userid, reason in cursor.fetchall():
        pending.setdefault(int(userid), []).append(reason)
    return pending


@contextmanager
def course_lock(workspace):
    """Hold the course's worker lock, or raise WorkerBusy."""
    workspace.worker.mkdir(parents=True, exist_ok=True)
    handle = (workspace.worker / "lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise WorkerBusy(f"another worker is running for course {workspace.course_id}") from None
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def next_full_run(now, at):
    """The next moment ``at`` ("HH:MM", local time) after ``now``; None if disabled."""
    if not at:
        return None
    hour, minute = (int(part) for part in at.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


class Worker:
    """One course's refresh loop. Everything external is injectable for tests."""

    def __init__(self, workspace, transport, connect, *, poll_seconds=15, full_at="03:00",
                 python=None, table_prefix="m_", clock=None, sleep=time.sleep,
                 refresh=refresh_learners, full=full_run, log=print):
        self.workspace = workspace
        self.transport = transport
        self.connect = connect
        self.poll_seconds = poll_seconds
        self.full_at = full_at
        self.python = python
        self.table_prefix = table_prefix
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.sleep = sleep
        self.refresh = refresh
        self.full = full
        self.log = log
        self.failures = 0
        self.next_full = next_full_run(self.clock(), full_at)

    # ------------------------------------------------------------------ state
    def _save_state(self, **entry):
        path = self.workspace.worker / "state.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        state.update(entry, updated_at=datetime.now(timezone.utc).isoformat())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")

    def _pending(self):
        connection = self.connect()
        try:
            cursor = connection.cursor()
            try:
                return pending_requests(cursor, self.workspace.course_id, self.table_prefix)
            finally:
                cursor.close()
        finally:
            connection.close()

    # ------------------------------------------------------------------ one tick
    def tick(self):
        """Do what is due now. Returns what happened: idle, refresh, full or error."""
        now = self.clock()
        try:
            if self.next_full is not None and now >= self.next_full:
                result = self.full(self.workspace, self.transport, self.python, log=self.log)
                self.next_full = next_full_run(now, self.full_at)
                self._save_state(last_full_run=result)
                self.failures = 0
                return "full"
            pending = self._pending()
            if not pending:
                return "idle"
            self.log(f"refresh requested for {sorted(pending)}: "
                     + ", ".join(f"{user} ({'/'.join(sorted(set(r)))})" for user, r in sorted(pending.items())))
            try:
                result = self.refresh(self.workspace, sorted(pending), self.transport, self.python,
                                      log=self.log)
            except Exception as error:  # noqa: BLE001 - decided by message below
                if not any(marker in str(error) for marker in FULL_RUN_ERRORS):
                    raise
                self.log(f"learner update refused ({error}); rebuilding the course instead")
                result = self.full(self.workspace, self.transport, self.python, log=self.log)
                self._save_state(last_full_run=result)
                self.failures = 0
                return "full"
            self._save_state(last_refresh=result)
            self.failures = 0
            return "refresh"
        except Exception as error:  # noqa: BLE001 - the loop must survive a bad tick
            self.failures += 1
            self.log(f"refresh failed ({self.failures} in a row): {error}")
            self._save_state(last_error={"at": now.isoformat(), "error": str(error)[-2000:],
                                         "consecutive_failures": self.failures})
            return "error"

    def pause_seconds(self):
        """Poll interval, growing after consecutive failures."""
        if not self.failures:
            return self.poll_seconds
        return min(self.poll_seconds * 2 ** self.failures, MAX_BACKOFF_SECONDS)

    def run(self, once=False, ticks=None):
        """Loop until interrupted (``once``: a single tick; ``ticks``: for tests)."""
        with course_lock(self.workspace):
            self.log(f"worker for course {self.workspace.course_id}: polling every "
                     f"{self.poll_seconds} s, full run daily at {self.full_at or 'never'}")
            done = 0
            while True:
                outcome = self.tick()
                done += 1
                if once or (ticks is not None and done >= ticks):
                    return outcome
                self.sleep(self.pause_seconds())
