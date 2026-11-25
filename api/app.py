"""Routes. Every endpoint except /health requires the shared X-API-Token.

Moodle (local_microlearning) is the only intended client. No CORS: browsers
never call this API, Moodle's PHP does.

    GET  /health                                         liveness, no learner data
    POST /v1/courses/{c}/events                          a learner's list may be stale -> refresh queued
    POST /v1/courses/{c}/learners/{u}/refresh?wait=1     recompute one learner -> bundle id
    POST /v1/courses/{c}/runs?wait=1                     rebuild the course -> bundle id
    GET  /v1/jobs?course_id=&limit=                      recent jobs, newest first (engine dashboard)
    GET  /v1/jobs/{id}                                   state of a queued or finished job
    GET  /v1/courses/{c}/status                          readiness, estimator in charge, last decision
    POST /v1/courses/{c}/train?population=               train the course model (deliberate; never forced)
    GET  /v1/courses/{c}/bundles/{id}.zip                a finished bundle (Moodle imports it)
    GET  /v1/courses/{c}/learners/{u}/profile            the learner's profile, as JSON
    GET  /v1/courses/{c}/learners/{u}/recommendations    the learner's queue, as JSON
    GET  /v1/courses/{c}/cohort                          cohort counts, as JSON
"""

import json
import math
import secrets
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParam, Query, Response
from pydantic import BaseModel

from course_workspace import WorkspaceError, default_root

from . import API_VERSION, bundles
from .jobs import DONE, FAILED, KIND_FULL, KIND_REFRESH, KIND_TRAIN, JobManager
from .settings import Settings

EVENT_TYPES = ("list_done", "quiz_failed", "new_learner", "manual")


class Event(BaseModel):
    userid: int
    type: str
    time: int | None = None


def _clean(value):
    """NaN / inf -> None so the payload is valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _records(frame):
    return [{k: _clean(v) for k, v in row.items()} for row in frame.to_dict(orient="records")]


def _read(path, learner=None):
    path = Path(path)
    if not path.is_file():
        return pd.DataFrame()
    frame = pd.read_csv(path, dtype={"learner_id": "string"})
    return frame[frame["learner_id"].eq(str(learner))] if learner is not None else frame


def _latest_layout(workspace, learner):
    """The newest run holding this learner: a refresh after the last full run, else the course."""
    built = workspace.learner_model / "build_manifest.json"
    full_time = json.loads(built.read_text())["generated_at"] if built.is_file() else ""
    for run in sorted(workspace.refresh.glob("*"), reverse=True):
        manifest = run / "learner_model" / "build_manifest.json"
        if not manifest.is_file() or json.loads(manifest.read_text())["generated_at"] <= full_time:
            continue
        if not _read(run / "learner_model" / "learner_profile.csv", learner).empty:
            return run
    return workspace.base


def _course_status(space):
    """Readiness per population and the recorded decision, plus the time of the last full run."""
    from knowledge_engine.phase import status

    report = status(space.data, space.active_model_file)
    model = report.pop("active_model") or {}
    gate = model.get("gate") or {}
    built = space.learner_model / "build_manifest.json"
    report.pop("data_dir", None)
    report["course_id"] = space.course_id
    report["active_model"] = {
        "phase": model.get("phase"),
        "estimator": model.get("estimator"),
        # The population the model was trained on; "cohort" only describes the data's mix.
        "population": model.get("training_population") or model.get("cohort"),
        "decided_at": model.get("decided_at"),
        "reason": model.get("reason"),
        "mapping_version": model.get("mapping_version"),
        "deep_model": (model.get("deep_model") or {}).get("model_name"),
        "gate_margin": model.get("gate_min_auc_margin"),
        "gate": {
            "deep_auc": (gate.get("deep") or {}).get("auc"),
            "bkt_auc": (gate.get("bkt") or {}).get("auc"),
            "n_targets": gate.get("n_targets"),
            "n_test_learners": gate.get("n_test_learners"),
        } if gate else None,
    }
    report["last_full_run"] = json.loads(built.read_text())["generated_at"] if built.is_file() else None
    return report


def create_app(settings=None, manager=None):
    settings = settings or Settings.from_env()
    manager = manager or JobManager(site=settings.site)
    # No interactive docs: the only client is Moodle, and nothing should be reachable without the token.
    app = FastAPI(title="Microlearning engine", version=API_VERSION, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.manager = manager

    def require_token(x_api_token: str | None = Header(default=None)):
        if x_api_token is None or not secrets.compare_digest(
            x_api_token.encode(), settings.api_token.encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid or missing API token")

    def workspace(course_id):
        try:
            space = manager.workspace(course_id)
        except WorkspaceError as error:
            raise HTTPException(status_code=404, detail=str(error))
        if not space.base.is_dir():
            raise HTTPException(status_code=404, detail=f"No workspace for course {course_id}")
        return space

    def answer(job, wait):
        """The job if it finished within the wait, else 202 with its id."""
        if wait:
            job.finished.wait(settings.wait_seconds)
        body = job.describe()
        if job.state == FAILED:
            raise HTTPException(status_code=502, detail=body)
        return Response(
            content=json.dumps(body), media_type="application/json",
            status_code=200 if job.state == DONE else 202,
        )

    auth = [Depends(require_token)]

    @app.get("/health")
    def health():
        root = default_root() / (settings.site or "local")
        courses = sorted(int(p.name) for p in root.glob("*") if p.name.isdigit() and p.is_dir())
        return {"status": "ok", "version": API_VERSION, "courses": courses,
                "active_jobs": len(manager.active())}

    @app.post("/v1/courses/{course_id}/events", dependencies=auth, status_code=202)
    def event(event: Event, course_id: int = PathParam(ge=2)):
        if event.type not in EVENT_TYPES:
            raise HTTPException(status_code=422, detail=f"type must be one of {EVENT_TYPES}")
        workspace(course_id)
        job = manager.submit(course_id, KIND_REFRESH, [event.userid], event.type)
        return {"queued": True, "job_id": job.id, "learners": sorted(job.learners)}

    @app.post("/v1/courses/{course_id}/learners/{userid}/refresh", dependencies=auth)
    def refresh(course_id: int = PathParam(ge=2), userid: int = PathParam(ge=1),
                wait: bool = Query(False)):
        workspace(course_id)
        return answer(manager.submit(course_id, KIND_REFRESH, [userid], "manual"), wait)

    @app.post("/v1/courses/{course_id}/runs", dependencies=auth)
    def run(course_id: int = PathParam(ge=2), wait: bool = Query(False)):
        workspace(course_id)
        return answer(manager.submit(course_id, KIND_FULL), wait)

    @app.get("/v1/jobs", dependencies=auth)
    def jobs(course_id: int | None = Query(None, ge=2), limit: int = Query(50, ge=1, le=500)):
        return {"jobs": manager.recent(course_id, limit), "active": len(manager.active())}

    @app.get("/v1/courses/{course_id}/status", dependencies=auth)
    def course_status(course_id: int = PathParam(ge=2)):
        space = workspace(course_id)
        try:
            report = _course_status(space)
        except FileNotFoundError as error:
            raise HTTPException(status_code=503, detail=f"No collected data yet; run the course first ({error})")
        report["pending_refresh"] = manager.runner(course_id).pending_learners()
        return report

    @app.post("/v1/courses/{course_id}/train", dependencies=auth)
    def train(course_id: int = PathParam(ge=2), population: str | None = Query(None),
              wait: bool = Query(False)):
        space = workspace(course_id)
        try:
            report = _course_status(space)
        except FileNotFoundError as error:
            raise HTTPException(status_code=503, detail=f"No collected data yet ({error})")
        chosen = population or report["training_population"]
        readiness = report["readiness_by_population"].get(chosen)
        if readiness is None:
            raise HTTPException(status_code=422, detail=f"No learners of population {chosen!r} in this course")
        if not readiness["ready"]:
            # The dashboard never forces training; the CLI's --force stays a deliberate, manual step.
            raise HTTPException(status_code=409, detail={"error": "course not ready", "unmet": readiness["unmet"]})
        return answer(manager.submit(course_id, KIND_TRAIN, options={"population": chosen}), wait)

    @app.get("/v1/jobs/{job_id}", dependencies=auth)
    def job(job_id: str):
        found = manager.job(job_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Unknown job (finished jobs are kept for a while)")
        return found.describe()

    @app.get("/v1/courses/{course_id}/bundles/{bundle_id}.zip", dependencies=auth)
    def bundle(course_id: int = PathParam(ge=2), bundle_id: str = PathParam(pattern="^[0-9a-f]{20}$")):
        directory = bundles.find(workspace(course_id), bundle_id)
        if directory is None:
            raise HTTPException(status_code=410, detail="Bundle replaced by a newer run; refresh again")
        return Response(
            content=bundles.zipped(directory), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{bundle_id}.zip"'},
        )

    @app.get("/v1/courses/{course_id}/learners/{userid}/profile", dependencies=auth)
    def profile(course_id: int = PathParam(ge=2), userid: int = PathParam(ge=1)):
        space = workspace(course_id)
        base = _latest_layout(space, userid)
        model = Path(base) / "learner_model"
        row = _read(model / "learner_profile.csv", userid)
        if row.empty:
            raise HTTPException(status_code=404, detail=f"No profile for learner {userid}")
        states = _read(model / "learner_kc_state.csv", userid)
        weak = _read(model / "weak_areas.csv", userid)
        if not weak.empty and "entity_type" in weak:
            weak = weak[weak["entity_type"].eq("kc")]
        levels = dict(zip(weak.get("entity_id", []), weak.get("knowledge_level", [])))
        concerns = dict(zip(weak.get("entity_id", []), weak.get("concern_status", [])))
        keep = ["kc_id", "kc_name", "kc_dimension", "attempt_count", "observed_accuracy",
                "predicted_correctness", "last_attempt_at", "estimator_phase"]
        kcs = states[[c for c in keep if c in states.columns]].assign(
            knowledge_level=lambda f: f["kc_id"].map(levels),
            concern_status=lambda f: f["kc_id"].map(concerns),
        )
        return {
            "learner_id": userid,
            "source": "refresh" if Path(base) != space.base else "full_run",
            "profile": _records(row)[0],
            "dimensions": _records(_read(model / "learner_dimension_summary.csv", userid)),
            "skills": _records(_read(model / "learner_skill_summary.csv", userid)),
            "kcs": _records(kcs),
            "quiz_attempts": _records(_read(model / "learner_quiz_attempts.csv", userid)),
        }

    @app.get("/v1/courses/{course_id}/learners/{userid}/recommendations", dependencies=auth)
    def recommendations(course_id: int = PathParam(ge=2), userid: int = PathParam(ge=1)):
        space = workspace(course_id)
        base = _latest_layout(space, userid)
        rows = _read(Path(base) / "recommendations" / "recommendations.csv", userid)
        if rows.empty:
            raise HTTPException(status_code=404, detail=f"No recommendations for learner {userid}")
        branch = rows["trigger_cmid"].notna() if "trigger_cmid" in rows else pd.Series(False, index=rows.index)
        columns = ["rank", "group", "trigger_cmid", "course_module_id", "activity_type", "activity_name",
                   "kc_id", "reason_code", "reason_text", "basis", "generated_at"]
        pick = lambda frame: _records(frame[[c for c in columns if c in frame.columns]])
        return {"learner_id": userid, "queue": pick(rows[~branch]), "failure_branches": pick(rows[branch])}

    @app.get("/v1/courses/{course_id}/cohort", dependencies=auth)
    def cohort(course_id: int = PathParam(ge=2)):
        space = workspace(course_id)
        profiles = _read(space.learner_model / "learner_profile.csv")
        if profiles.empty:
            raise HTTPException(status_code=503, detail="No learner model yet; run the course first")
        counts = lambda column: {str(k): int(v) for k, v in profiles[column].value_counts().items()} \
            if column in profiles else {}
        return {
            "course_id": course_id,
            "learners": int(len(profiles)),
            "by_population": counts("source_population"),
            "by_phase": counts("knowledge_phase"),
            "pending_refresh": manager.runner(course_id).pending_learners(),
        }

    return app
