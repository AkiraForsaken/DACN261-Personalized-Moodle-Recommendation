"""Load one consistent snapshot: learner state plus course structure.

Learner state comes from the learner-model build (``--profile-dir``):

    learner_profile.csv         who the learners are, population, estimator phase
    learner_kc_state.csv        attempts, observed accuracy, predicted correctness
    weak_areas.csv              concern status per KC and its basis
    learner_activity_state.csv  failed/passed quizzes, lesson views and their times
    learner_quiz_attempts.csv   every finished quiz attempt (time, passed)
    build_manifest.json         cutoff, mapping version, thresholds, input hashes

Course structure comes from the collector export (``--data-dir``):

    activity_catalog.csv, kc_activity_map.csv

and so does the view history (``learning_events.csv``, content views strictly
before the build's cutoff): "was this lesson read between two attempts" needs
every view, not only the last one.

The two must belong together: after a course rebuild every course-module id
changes, and profiles of the old course would point at activities that no
longer exist. The learner model recorded the SHA-256 of the collector files it
was built from, so this module checks them and refuses a mismatch.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import config
from .course import build_course_graph

STRUCTURE_FILES = {
    "activity_catalog": "activity_catalog.csv",
    "kc_activity_map": "kc_activity_map.csv",
}
DONE_STATUSES = ("completed", "passed")
CONTENT_VIEW = "content_view"


@dataclass
class ActivityState:
    """One learner's record on one activity (see learner_model.activity_state)."""

    status: str
    last_viewed: float | None = None  # Unix seconds
    last_attempt: float | None = None
    attempt_count: int = 0
    best_score: float | None = None
    pass_fraction: float | None = None
    # Quizzes: (finished, passed) of every attempt, oldest first.
    attempts: tuple = ()
    # Every view (Unix seconds), oldest first.
    view_times: tuple = ()

    def viewed_between(self, start, end):
        """Was the activity viewed after ``start`` and before ``end``?"""
        return any(start < t < end for t in self.view_times)

    @property
    def done(self):
        return self.status in DONE_STATUSES

    @property
    def last_touched(self):
        times = [t for t in (self.last_viewed, self.last_attempt) if t is not None]
        return max(times) if times else None


@dataclass
class KCState:
    """One learner's knowledge state and concern on one KC."""

    attempts: int = 0
    observed_accuracy: float | None = None
    predicted_correctness: float | None = None
    last_attempt: float | None = None
    concern: str | None = None  # weak_areas.concern_status

    @property
    def correct(self):
        """Number of correct answers behind the observed accuracy."""
        if self.observed_accuracy is None:
            return None
        return int(round(self.observed_accuracy * self.attempts))


@dataclass
class LearnerState:
    learner_id: str
    population: str | None = None
    phase: int | None = None  # estimator phase of the knowledge component
    activities: dict = field(default_factory=dict)  # module_id -> ActivityState
    kcs: dict = field(default_factory=dict)  # kc_id -> KCState

    def activity(self, module_id):
        return self.activities.get(module_id)

    def is_done(self, module_id):
        state = self.activities.get(module_id)
        return state is not None and state.done

    @property
    def last_touched_module(self):
        """The activity the learner worked on most recently, or None."""
        touched = [
            (state.last_touched, module_id)
            for module_id, state in self.activities.items()
            if state.last_touched is not None
        ]
        return max(touched)[1] if touched else None


@dataclass
class RecommenderInputs:
    course: object  # CourseGraph
    learners: dict  # learner_id -> LearnerState, in profile order
    low_accuracy: float
    as_of: str | None
    mapping_version: str | None
    input_files: dict  # name -> {"path", "sha256"} for the manifest


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path, **kwargs):
    path = Path(path)
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, **kwargs)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _seconds(series):
    """ISO timestamps -> Unix seconds as floats (NaN where missing)."""
    stamps = pd.to_datetime(series, errors="coerce", utc=True)
    seconds = (stamps - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
    return seconds


def _value(number):
    return None if number is None or pd.isna(number) else float(number)


def _check_same_export(manifest, data_dir):
    """The profiles and the course structure must come from one collector export."""
    recorded = manifest.get("inputs", {})
    for name, filename in STRUCTURE_FILES.items():
        path = Path(data_dir) / filename
        if name not in recorded or not path.is_file():
            continue
        if recorded[name]["sha256"] != sha256_file(path):
            raise ValueError(
                f"{path} is not the file the learner profiles were built from "
                "(the course export changed since). Re-run `python -m "
                "knowledge_engine estimate` and `python -m learner_model.builder` "
                "on this data directory first."
            )


def _attach_history(learners, quiz_attempts, data_dir, as_of):
    """Fill every ActivityState's attempt history and view times.

    The attempts come from the learner-model build (already cut at its
    ``as_of``); the views from the collector's events, cut here the same way:
    strictly before ``as_of``.
    """
    cutoff = None
    if as_of:
        cutoff = _seconds(pd.Series([as_of])).iloc[0]
    if not quiz_attempts.empty:
        frame = quiz_attempts.assign(
            finished=_seconds(quiz_attempts["finished_at"]),
            passed_flag=pd.to_numeric(quiz_attempts["passed"], errors="coerce").fillna(0).astype(int),
            cmid=pd.to_numeric(quiz_attempts["course_module_id"], errors="coerce"),
        ).dropna(subset=["finished", "cmid"])
        for (learner_id, cmid), rows in frame.sort_values("finished").groupby(
            ["learner_id", "cmid"], sort=False
        ):
            state = learners.get(str(learner_id), None)
            state = state.activities.get(int(cmid)) if state else None
            if state is not None:
                state.attempts = tuple(
                    (float(t), bool(p)) for t, p in zip(rows["finished"], rows["passed_flag"])
                )
    events = _read(
        Path(data_dir) / "learning_events.csv",
        usecols=lambda c: c in {"learner_id", "course_module_id", "event_category", "timestamp_unix"},
        dtype={"learner_id": "string"},
    )
    if events.empty or "event_category" not in events.columns:
        return
    views = events[events["event_category"].eq(CONTENT_VIEW)].assign(
        cmid=lambda f: pd.to_numeric(f["course_module_id"], errors="coerce"),
        t=lambda f: pd.to_numeric(f["timestamp_unix"], errors="coerce"),
    ).dropna(subset=["cmid", "t"])
    if cutoff is not None and not pd.isna(cutoff):
        views = views[views["t"] < cutoff]
    for (learner_id, cmid), rows in views.sort_values("t").groupby(["learner_id", "cmid"], sort=False):
        state = learners.get(str(learner_id), None)
        state = state.activities.get(int(cmid)) if state else None
        if state is not None:
            state.view_times = tuple(float(t) for t in rows["t"])


def load_inputs(profile_dir, data_dir, verify_same_export=True):
    """Read the learner-model build and the course structure into memory."""
    profile_dir, data_dir = Path(profile_dir), Path(data_dir)
    manifest_path = profile_dir / "build_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"No learner-model build in {profile_dir}. Run `python -m "
            "learner_model.builder` first."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog_path = data_dir / STRUCTURE_FILES["activity_catalog"]
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Activity catalog not found: {catalog_path}")
    if verify_same_export:
        _check_same_export(manifest, data_dir)

    text = {"learner_id": "string"}
    profiles = _read(profile_dir / "learner_profile.csv", dtype=text)
    kc_states = _read(profile_dir / "learner_kc_state.csv", dtype={**text, "kc_id": "string"})
    weak_areas = _read(profile_dir / "weak_areas.csv", dtype={**text, "entity_id": "string"})
    activity_state = _read(profile_dir / "learner_activity_state.csv", dtype=text)
    quiz_attempts = _read(profile_dir / "learner_quiz_attempts.csv", dtype=text)
    if profiles.empty:
        raise ValueError(f"{profile_dir / 'learner_profile.csv'} holds no learner")

    kc_names = (
        dict(zip(kc_states["kc_id"].astype(str), kc_states["kc_name"].astype(str)))
        if {"kc_id", "kc_name"} <= set(kc_states.columns)
        else {}
    )
    kc_dimension = (
        dict(zip(kc_states["kc_id"].astype(str), kc_states["kc_dimension"].astype(str)))
        if {"kc_id", "kc_dimension"} <= set(kc_states.columns)
        else {}
    )
    course = build_course_graph(
        pd.read_csv(catalog_path),
        _read(data_dir / STRUCTURE_FILES["kc_activity_map"], dtype={"kc_id": "string"}),
        kc_names,
        kc_dimension,
    )

    learners = {}
    for row in profiles.itertuples(index=False):
        phase = getattr(row, "knowledge_phase", None)
        learners[str(row.learner_id)] = LearnerState(
            learner_id=str(row.learner_id),
            population=getattr(row, "source_population", None),
            phase=None if phase is None or pd.isna(phase) else int(phase),
        )

    if not activity_state.empty:
        activity_state = activity_state.assign(
            viewed_seconds=_seconds(activity_state["last_viewed_at"]),
            attempt_seconds=_seconds(activity_state["last_attempt_at"]),
        )
        for row in activity_state.itertuples(index=False):
            learner = learners.get(str(row.learner_id))
            if learner is None:
                continue
            learner.activities[int(row.course_module_id)] = ActivityState(
                status=str(row.status),
                last_viewed=_value(row.viewed_seconds),
                last_attempt=_value(row.attempt_seconds),
                attempt_count=int(row.attempt_count or 0),
                best_score=_value(row.best_score),
                pass_fraction=_value(row.pass_fraction),
            )

    _attach_history(learners, quiz_attempts, data_dir, manifest.get("as_of"))

    concerns = {}
    if not weak_areas.empty:
        kc_rows = (
            weak_areas[weak_areas["entity_type"].eq("kc")]
            if "entity_type" in weak_areas.columns
            else weak_areas
        )
        concerns = dict(
            zip(
                zip(kc_rows["learner_id"].astype(str), kc_rows["entity_id"].astype(str)),
                kc_rows["concern_status"],
            )
        )
    if not kc_states.empty:
        kc_states = kc_states.assign(
            attempt_seconds=_seconds(kc_states["last_attempt_at"])
        )
        for row in kc_states.itertuples(index=False):
            learner = learners.get(str(row.learner_id))
            if learner is None:
                continue
            kc_id = str(row.kc_id)
            learner.kcs[kc_id] = KCState(
                attempts=int(row.attempt_count or 0),
                observed_accuracy=_value(row.observed_accuracy),
                predicted_correctness=_value(row.predicted_correctness),
                last_attempt=_value(row.attempt_seconds),
                concern=concerns.get((learner.learner_id, kc_id)),
            )

    input_files = {
        name: {"path": str(path), "sha256": sha256_file(path)}
        for name, path in {
            "build_manifest": manifest_path,
            "learner_profile": profile_dir / "learner_profile.csv",
            "learner_kc_state": profile_dir / "learner_kc_state.csv",
            "weak_areas": profile_dir / "weak_areas.csv",
            "learner_activity_state": profile_dir / "learner_activity_state.csv",
            "learner_quiz_attempts": profile_dir / "learner_quiz_attempts.csv",
            "learning_events": data_dir / "learning_events.csv",
            "activity_catalog": catalog_path,
            "kc_activity_map": data_dir / STRUCTURE_FILES["kc_activity_map"],
        }.items()
        if Path(path).is_file()
    }
    return RecommenderInputs(
        course=course,
        learners=learners,
        low_accuracy=float(
            manifest.get("configuration", {}).get(
                "low_observed_accuracy_threshold", config.LOW_OBSERVED_ACCURACY
            )
        ),
        as_of=manifest.get("as_of"),
        mapping_version=manifest.get("mapping", {}).get("version"),
        input_files=input_files,
    )
