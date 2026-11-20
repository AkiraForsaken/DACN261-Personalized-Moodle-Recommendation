"""Refresh named learners only, or the whole course, and publish the result.

    refresh_learners  collector --learners -> model stages (learner_update) -> push
    full_run          collector            -> model stages (snapshot)       -> push

The collector runs here (it needs the MariaDB driver, .venv); the model stages
run as ``python -m moodle_sync stages`` in the PyTorch environment
(``model_python``, default .venv-kt), the same split as run_pipeline.sh.
A per-learner run happens in ``refresh/<stamp>/`` of the course workspace and
reuses the course's estimator decision and trained model; it never trains.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from course_workspace import PROJECT_ROOT

from .bundle import KIND_LEARNER_UPDATE, KIND_SNAPSHOT

DEFAULT_MODEL_PYTHON = PROJECT_ROOT / ".venv-kt" / "bin" / "python"
KEEP_REFRESH_RUNS = 20


def model_python(path=None):
    """The interpreter that runs the model stages (needs PyTorch if a deep model is promoted)."""
    if path:
        return str(path)
    return str(DEFAULT_MODEL_PYTHON) if DEFAULT_MODEL_PYTHON.is_file() else sys.executable


def run_stages(workspace, run_dir, kind, python=None):
    """Run the model stages for ``run_dir`` in a subprocess; return the bundle manifest."""
    command = [
        model_python(python), "-m", "moodle_sync", "stages",
        "--course", str(workspace.course_id), "--site", workspace.site,
        "--run-dir", str(run_dir), "--kind", kind,
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"model stages failed (exit {result.returncode}):\n{result.stderr.strip()[-2000:]}"
        )
    return json.loads((Path(run_dir) / "bundle" / "manifest.json").read_text(encoding="utf-8"))


def _prune(workspace, keep=KEEP_REFRESH_RUNS):
    runs = sorted(path for path in workspace.refresh.glob("*") if path.is_dir())
    for old in runs[:-keep] if keep else runs:
        shutil.rmtree(old, ignore_errors=True)


def refresh_learners(workspace, learners, transport, python=None, collect=None, log=print):
    """Recompute and publish the recommendations of ``learners`` only.

    Returns a summary: learners, timings, bundle row counts and the plugin's
    import result. ``collect`` is the collector entry point (replaced in tests).
    """
    if collect is None:
        from collector import collect
    learners = sorted({int(learner) for learner in learners})
    if not learners:
        raise ValueError("No learners to refresh")
    started = time.monotonic()
    run = workspace.refresh_run()
    collect(workspace.course_id, workspace.site, run.data, learners)
    collected = time.monotonic()
    manifest = run_stages(workspace, run.base, KIND_LEARNER_UPDATE, python)
    modelled = time.monotonic()
    imported = transport.push(run.bundle, workspace.course_id)
    finished = time.monotonic()
    _prune(workspace)
    summary = {
        "kind": KIND_LEARNER_UPDATE,
        "learners": learners,
        "run_dir": str(run.base),
        "seconds": {
            "collect": round(collected - started, 2),
            "model": round(modelled - collected, 2),
            "import": round(finished - modelled, 2),
            "total": round(finished - started, 2),
        },
        "rows": {name: entry["rows"] for name, entry in manifest["files"].items()},
        "import": imported,
    }
    log(f"refreshed learners {learners} in {summary['seconds']['total']} s "
        f"(collect {summary['seconds']['collect']}, model {summary['seconds']['model']}, "
        f"import {summary['seconds']['import']})")
    return summary


def full_run(workspace, transport, python=None, collect=None, log=print):
    """Collect the whole course, rebuild everything and publish a new snapshot."""
    if collect is None:
        from collector import collect
    started = time.monotonic()
    collect(workspace.course_id, workspace.site, workspace.data, None)
    manifest = run_stages(workspace, workspace.base, KIND_SNAPSHOT, python)
    imported = transport.push(workspace.bundle, workspace.course_id)
    seconds = round(time.monotonic() - started, 2)
    log(f"full run of course {workspace.course_id} in {seconds} s")
    return {
        "kind": KIND_SNAPSHOT,
        "seconds": {"total": seconds},
        "rows": {name: entry["rows"] for name, entry in manifest["files"].items()},
        "import": imported,
    }
