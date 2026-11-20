"""Build learner profiles for one course from its own Moodle evidence.

Inputs (all from the course, none from a benchmark dataset):

    <data-dir>/*.csv               Moodle collector exports
    learner_kc_state.csv           knowledge-engine contract (optional)

Every enrolled learner gets a profile. Without a knowledge-state file the
knowledge, skills and weak-area components are ``unavailable`` and the rest of
the profile is still built; with it, a learner who has answered nothing has a
``cold_start_only`` knowledge component (priors, not evidence).
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .activity_state import (
    ACTIVITY_STATUSES,
    build_activity_state,
    build_quiz_attempt_history,
)
from .behavior import build_behavior
from .config import (
    HIGH_PREDICTED_CORRECTNESS,
    KC_CATALOG_FILE,
    KC_TOPIC_MAP_FILE,
    LOW_OBSERVED_ACCURACY,
    LOW_PREDICTED_CORRECTNESS,
    MIN_ATTEMPTS_FOR_EVIDENCE_CONCERN,
    MIXED_COHORT,
    MOODLE_POPULATION,
    PROFILE_SCHEMA_VERSION,
    ROSTER_FILE,
    SIMULATED_POPULATION,
    SIMULATION_MANIFEST_NAME,
    UNVERSIONED_MAPPING,
)
from .engagement import build_engagement
from .history import build_history
from .knowledge import load_knowledge_states, summarize_dimensions
from .profile import USABLE_STATUSES, build_profiles
from .schemas import PROFILE_COLUMNS
from .skills import (
    build_kc_skill_map,
    ensure_kc_coverage,
    load_kc_catalog,
    load_kc_topic_map,
    summarize_skills,
)
from .temporal import truncate_frames
from .knowledge_levels import KNOWLEDGE_LEVELS, LEVEL_WEAK
from .weak_areas import CONCERN_STATUSES, identify_weak_areas

COLLECTOR_FILES = {
    "activity_catalog": "activity_catalog.csv",
    "learning_events": "learning_events.csv",
    "quiz_attempts": "quiz_attempts.csv",
    "course_modules_completion": "course_modules_completion.csv",
}
OPTIONAL_INPUT_FILES = {
    "learner_roster": ROSTER_FILE,
    "kc_catalog": KC_CATALOG_FILE,
    "kc_topic_map": KC_TOPIC_MAP_FILE,
    "kc_activity_map": "kc_activity_map.csv",
    "question_answer_events": "question_answer_events.csv",
    "moodle_question_kc_mapping": "moodle_question_kc_map.csv",
    "question_answer_mapping_report": "question_answer_mapping_report.csv",
    "simulation_manifest": SIMULATION_MANIFEST_NAME,
}


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _with_population(frame, population_of):
    """Label every row with its learner's population (``population_of(id)``)."""
    result = frame.copy()
    if "learner_id" in result.columns:
        values = result["learner_id"].astype(str).map(population_of).astype(object)
    else:
        values = pd.Series(index=result.index, dtype=object)
    if "source_population" in result.columns:
        result["source_population"] = values
    else:
        result.insert(0, "source_population", values)
    return result


def detect_population(data_dir):
    """Population of a learner the roster does not label.

    A directory holding a simulation manifest contains simulated learners, so
    an unlabelled learner in it is never reported as real Moodle data.
    """
    if (Path(data_dir) / SIMULATION_MANIFEST_NAME).is_file():
        return SIMULATED_POPULATION
    return MOODLE_POPULATION


def roster_populations(roster):
    """``{learner_id: population}`` from the roster's ``is_simulated`` column.

    The collector labels each learner there, so a real learner enrolled next
    to simulated ones stays ``moodle_collector``. A roster without the column
    (older exports) labels nobody and ``detect_population`` applies.
    """
    if roster.empty or not {"learner_id", "is_simulated"} <= set(roster.columns):
        return {}
    flags = pd.to_numeric(roster["is_simulated"], errors="coerce")
    if flags.isna().any() or not flags.isin([0, 1]).all():
        raise ValueError("Learner roster: is_simulated must be 0 or 1 on every row")
    labels = flags.map({1: SIMULATED_POPULATION, 0: MOODLE_POPULATION})
    return dict(zip(roster["learner_id"].astype(str), labels))


def _read_optional(path, **kwargs):
    """A collector export that does not exist yet is an empty table, not an error."""
    return pd.read_csv(path, **kwargs) if Path(path).is_file() else pd.DataFrame()


def _load_collector_frames(data_dir):
    """Load the Moodle collector exports used by behavioral components."""
    activities = _read_optional(data_dir / COLLECTOR_FILES["activity_catalog"])
    events = _read_optional(data_dir / COLLECTOR_FILES["learning_events"])
    quizzes = _read_optional(data_dir / COLLECTOR_FILES["quiz_attempts"])
    completions = _read_optional(
        data_dir / COLLECTOR_FILES["course_modules_completion"]
    )
    roster = _read_optional(data_dir / ROSTER_FILE, dtype={"learner_id": "string"})
    return activities, events, quizzes, completions, roster


def _active_roster_ids(roster, cutoff_unix):
    """Learners enrolled at the cutoff, including those with no activity yet."""
    if roster.empty:
        return set()
    if "learner_id" not in roster or roster["learner_id"].isna().any():
        raise ValueError("Learner roster must have non-empty learner_id values")
    if {"enrol_start_unix", "enrol_end_unix"}.issubset(roster.columns):
        cutoff = (
            cutoff_unix
            if cutoff_unix is not None
            else int(pd.Timestamp.now(tz="UTC").timestamp())
        )
        starts = pd.to_numeric(roster["enrol_start_unix"], errors="coerce").fillna(0)
        ends = pd.to_numeric(roster["enrol_end_unix"], errors="coerce").fillna(0)
        roster = roster.loc[starts.le(cutoff) & (ends.eq(0) | ends.gt(cutoff))]
    return set(roster["learner_id"].dropna().astype(str))


def _latest_timestamp_map(frames):
    latest = {}
    for frame, time_column, is_unix in frames:
        if frame.empty or not {"learner_id", time_column}.issubset(frame.columns):
            continue
        times = (
            pd.to_datetime(
                pd.to_numeric(frame[time_column], errors="coerce"),
                unit="s",
                errors="coerce",
                utc=True,
            )
            if is_unix
            else pd.to_datetime(frame[time_column], errors="coerce", utc=True)
        )
        current = pd.DataFrame(
            {"learner_id": frame["learner_id"].astype(str), "timestamp": times}
        ).dropna(subset=["timestamp"])
        for learner_id, timestamp in (
            current.groupby("learner_id")["timestamp"].max().items()
        ):
            previous = latest.get(learner_id)
            if previous is None or timestamp > previous:
                latest[learner_id] = timestamp
    return {
        learner_id: timestamp.isoformat() for learner_id, timestamp in latest.items()
    }


def _time_column(frame, unix_name, text_name):
    """(frame, column, is_unix) for the timestamp column the frame carries."""
    return (frame, unix_name, True) if unix_name in frame else (frame, text_name, False)


def normalize_as_of(as_of):
    """Return ``(iso_utc, unix_seconds)`` for a cutoff, or ``(None, None)``.

    A cutoff must be Unix seconds or a timezone-aware datetime string. Naive
    strings are rejected: the knowledge loader would read them as UTC while the
    collector helper reads them as collector-local time, so the components
    would silently use different instants.
    """
    if as_of is None:
        return None, None
    if isinstance(as_of, (int, float)) and not isinstance(as_of, bool):
        stamp = pd.Timestamp(int(as_of), unit="s", tz="UTC")
    elif isinstance(as_of, str) and as_of.strip().lstrip("-").isdigit():
        stamp = pd.Timestamp(int(as_of), unit="s", tz="UTC")
    else:
        stamp = pd.Timestamp(as_of)
        if pd.isna(stamp):
            raise ValueError(f"Invalid as_of cutoff: {as_of!r}")
        if stamp.tzinfo is None:
            raise ValueError(
                "as_of must be Unix seconds or a timezone-aware ISO datetime "
                f"(e.g. 2026-12-01T00:00:00Z); got {as_of!r}"
            )
        stamp = stamp.tz_convert("UTC")
    return stamp.isoformat(), int(stamp.timestamp())


def _duplicates(frame, keys):
    return not frame.empty and frame.duplicated(keys).any()


def validate_outputs(outputs, as_of):
    """Raise on structural or semantic violations before artifacts are written.

    ``as_of`` is the normalized ISO cutoff (or None).
    """
    failures = []
    checks = []

    def check(passed, ok_message, fail_message):
        if passed:
            checks.append(ok_message)
        else:
            failures.append(fail_message)

    profiles = outputs["learner_profile"]
    missing_profile_columns = set(PROFILE_COLUMNS).difference(profiles.columns)
    check(
        not missing_profile_columns,
        "profile schema contains every required column",
        f"profile schema is missing columns: {sorted(missing_profile_columns)}",
    )
    check(
        not _duplicates(profiles, ["source_population", "learner_id", "as_of"]),
        "profile keys are unique within the snapshot",
        "duplicate population/learner/snapshot profiles",
    )

    kc_states = outputs["learner_kc_state"]
    check(
        not _duplicates(kc_states, ["source_population", "learner_id", "kc_id"]),
        "learner-KC keys are unique",
        "duplicate source-population/learner/KC states",
    )
    for name, keys in {
        "learner_skill_summary": ["source_population", "learner_id", "skill_id"],
        "learner_dimension_summary": [
            "source_population",
            "learner_id",
            "kc_dimension",
        ],
        "learner_behavior": ["source_population", "learner_id"],
        "learner_engagement": ["source_population", "learner_id"],
        "learner_history": ["source_population", "learner_id"],
        "learner_activity_state": [
            "source_population",
            "learner_id",
            "course_module_id",
        ],
        "weak_areas": ["source_population", "learner_id", "entity_type", "entity_id"],
        "learner_quiz_attempts": [
            "source_population",
            "learner_id",
            "course_module_id",
            "attempt",
        ],
    }.items():
        check(
            not _duplicates(outputs[name], keys),
            f"{name} keys are unique",
            f"duplicate keys in {name} on {keys}",
        )

    range_ok = True
    for column in ("observed_accuracy", "predicted_correctness", "p_mastery"):
        if column in kc_states:
            values = pd.to_numeric(kc_states[column], errors="coerce")
            if (values.notna() & ~values.between(0, 1)).any():
                failures.append(f"{column} is outside [0, 1]")
                range_ok = False
    if range_ok:
        checks.append("knowledge probabilities are within [0, 1]")
    for name, column in (
        ("learner_behavior", "revisit_rate"),
        ("learner_engagement", "topic_completion_rate"),
        ("learner_engagement", "course_progress"),
    ):
        frame = outputs[name]
        if column in frame:
            values = pd.to_numeric(frame[column], errors="coerce")
            if (values.notna() & ~values.between(0, 1)).any():
                failures.append(f"{name}.{column} is outside [0, 1]")
    count_columns = {
        "learner_behavior": [
            "session_count",
            "learning_activity_count",
            "practice_activity_count",
            "review_activity_count",
        ],
        "learner_engagement": [
            "active_session_count",
            "total_learning_events",
            "total_activity_views",
            "estimated_learning_time_seconds",
        ],
        "learner_history": [
            "total_learning_events",
            "total_quiz_attempts",
            "historical_learning_days",
        ],
    }
    for name, columns in count_columns.items():
        for column in columns:
            if column in outputs[name]:
                values = pd.to_numeric(outputs[name][column], errors="coerce")
                if values.lt(0).any():
                    failures.append(f"{name}.{column} contains negative values")

    # Cold-start rows must never carry a fabricated observed accuracy of 0.
    if {"state_status", "observed_accuracy", "attempt_count"}.issubset(
        kc_states.columns
    ):
        no_attempts = pd.to_numeric(kc_states["attempt_count"], errors="coerce").eq(0)
        check(
            kc_states.loc[no_attempts, "observed_accuracy"].isna().all(),
            "KC rows without direct attempts have no observed accuracy",
            "KC rows with zero attempts carry an observed_accuracy value",
        )

    if as_of is not None and not kc_states.empty:
        attempts = pd.to_datetime(
            kc_states["last_attempt_at"], errors="coerce", utc=True
        )
        has_attempts = pd.to_numeric(kc_states["attempt_count"], errors="coerce").gt(0)
        bad = has_attempts & (attempts.isna() | attempts.gt(pd.Timestamp(as_of)))
        check(
            not bad.any(),
            "KC last-attempt times respect the snapshot cutoff",
            "KC states contain a missing or post-cutoff last_attempt_at",
        )

    # Cross-component: every component row belongs to a profile.
    keys = set(zip(profiles["source_population"], profiles["learner_id"].astype(str)))
    orphan = []
    for name in (
        "learner_kc_state",
        "learner_skill_summary",
        "learner_dimension_summary",
        "learner_behavior",
        "learner_engagement",
        "learner_history",
        "learner_activity_state",
        "weak_areas",
    ):
        frame = outputs[name]
        if frame.empty:
            continue
        pairs = set(zip(frame["source_population"], frame["learner_id"].astype(str)))
        if pairs - keys:
            orphan.append(name)
    check(
        not orphan,
        "every component row belongs to a profile",
        f"component rows without a matching profile: {orphan}",
    )

    weak = outputs["weak_areas"]
    kc_keys = set(
        zip(
            kc_states["source_population"],
            kc_states["learner_id"].astype(str),
            kc_states["kc_id"].astype(str),
        )
    )
    weak_keys = set(
        zip(
            weak["source_population"],
            weak["learner_id"].astype(str),
            weak["entity_id"].astype(str),
        )
    )
    check(
        weak_keys == kc_keys,
        "every weak-area row traces to a learner-KC state and vice versa",
        "weak-area rows do not match learner-KC states one-to-one",
    )
    if "knowledge_level" in weak:
        unknown_levels = set(weak["knowledge_level"].dropna()) - set(KNOWLEDGE_LEVELS)
        weak_level = weak["knowledge_level"].eq(LEVEL_WEAK)
        evidence = weak["concern_status"].eq("evidence_based_concern")
        check(
            not unknown_levels and weak_level.eq(evidence).all(),
            "knowledge levels are valid and 'weak' is exactly the evidence-based concern",
            f"knowledge levels disagree with evidence-based concerns ({sorted(unknown_levels)})",
        )
    invalid_concerns = set(weak["concern_status"].dropna()) - set(CONCERN_STATUSES)
    check(
        not invalid_concerns and weak["reason"].notna().all(),
        "concern statuses are valid and every row has a reason",
        f"invalid concern statuses or missing reasons: {sorted(invalid_concerns)}",
    )
    if not kc_states.empty and "state_status" in kc_states:
        merged = weak.merge(
            kc_states[["source_population", "learner_id", "kc_id", "attempt_count"]],
            left_on=["source_population", "learner_id", "entity_id"],
            right_on=["source_population", "learner_id", "kc_id"],
        )
        cold_evidence = merged["attempt_count"].eq(0) & merged["concern_status"].eq(
            "evidence_based_concern"
        )
        check(
            not cold_evidence.any(),
            "no cold-start KC is labelled an evidence-based concern",
            "cold-start KC labelled evidence_based_concern",
        )

    skills = outputs["learner_skill_summary"]
    if not skills.empty:
        check(
            skills["kc_count"].gt(0).all()
            and skills["estimated_kc_count"]
            .add(skills["cold_start_kc_count"])
            .eq(skills["kc_count"])
            .all(),
            "skill summaries trace to their KC rows (estimated + cold-start = KC count)",
            "skill summary KC counts are inconsistent",
        )

    activity = outputs["learner_activity_state"]
    if not activity.empty:
        unknown_statuses = set(activity["status"]) - set(ACTIVITY_STATUSES)
        check(
            not unknown_statuses,
            "activity statuses are valid",
            f"unknown activity statuses: {sorted(unknown_statuses)}",
        )
        from_attempts = activity["status_source"].eq("attempts")
        check(
            activity.loc[from_attempts, "attempt_count"].gt(0).all(),
            "quiz outcomes reconstructed from attempts have at least one attempt",
            "a quiz outcome is attributed to attempts but has none",
        )
        views = pd.to_datetime(activity["last_viewed_at"], errors="coerce", utc=True)
        first = pd.to_datetime(activity["first_viewed_at"], errors="coerce", utc=True)
        check(
            not (first > views).any(),
            "activity view times are ordered",
            "an activity's first view lies after its last view",
        )

    # A cohort may mix real and simulated learners, but a learner is one or the
    # other in every output.
    seen, unlabelled, conflicting = {}, [], set()
    for name, frame in outputs.items():
        if frame.empty or not {"source_population", "learner_id"} <= set(frame.columns):
            continue
        if frame["source_population"].isna().any():
            unlabelled.append(name)
        pairs = set(zip(frame["learner_id"].astype(str), frame["source_population"]))
        for learner_id, population in pairs:
            if seen.setdefault(learner_id, population) != population:
                conflicting.add(learner_id)
    check(
        not unlabelled and not conflicting,
        "every learner belongs to exactly one source population in all outputs",
        f"rows without a population in {unlabelled}; learners under several "
        f"populations: {sorted(conflicting)[:10]}",
    )

    valid_status = USABLE_STATUSES | {"unavailable", "no_observations"}
    for column in [c for c in profiles.columns if c.endswith("_status")]:
        if set(profiles[column].dropna()) - valid_status:
            failures.append(f"profile.{column} has unknown status values")

    if failures:
        raise ValueError("Learner-model validation failed: " + "; ".join(failures))
    return checks


def _empty_knowledge():
    states = pd.DataFrame(
        columns=[
            "learner_id",
            "kc_id",
            "kc_dimension",
            "attempt_count",
            "evidence_count",
            "observed_accuracy",
            "predicted_correctness",
            "p_mastery",
            "last_attempt_at",
            "state_status",
            "model_name",
            "model_version",
            "estimator_phase",
        ]
    )
    states.attrs.update(
        source_learner_ids=[],
        learner_populations={},
        knowledge_snapshot_as_of=None,
        dropped_after_cutoff=0,
        dropped_without_timestamp=0,
    )
    return states


def _counts(series):
    return {str(k): int(v) for k, v in series.value_counts().items()}


def build_complete_learner_model(
    data_dir,
    output_dir,
    knowledge_state_file,
    as_of=None,
    generated_at=None,
):
    """Build every learner-model component for the course in ``data_dir``.

    ``knowledge_state_file`` is the knowledge-engine contract. If it does not
    exist, profiles are still built and knowledge is ``unavailable``. Knowledge
    states of another population than ``data_dir`` are rejected, so simulated
    learners are never joined to real Moodle activity.

    ``as_of`` cuts off every time-dependent component. ``generated_at`` may be
    pinned so that two builds of the same inputs are byte-identical.
    """
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    knowledge_state_file = Path(knowledge_state_file)
    as_of_iso, as_of_unix = normalize_as_of(as_of)
    if not data_dir.is_dir():
        raise FileNotFoundError(
            f"Learner-model data directory does not exist: {data_dir}"
        )
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    activities, events, quizzes, completions, roster = _load_collector_frames(data_dir)
    # Each learner's population: the roster's label, else the directory's.
    labelled = roster_populations(roster)
    default_population = detect_population(data_dir)

    def population_of(learner_id):
        return labelled.get(str(learner_id), default_population)

    # ---- knowledge, skills, weak areas (from the knowledge-engine contract)
    knowledge_available = knowledge_state_file.is_file()
    if knowledge_available:
        states = load_knowledge_states(knowledge_state_file, as_of=as_of_iso)
        mismatched = {
            learner_id: state_population
            for learner_id, state_population in states.attrs[
                "learner_populations"
            ].items()
            if state_population != population_of(learner_id)
        }
        if mismatched:
            learner_id, state_population = sorted(mismatched.items())[0]
            raise ValueError(
                f"Knowledge states describe {len(mismatched)} learner(s) under another "
                f"population than {data_dir} does (e.g. learner {learner_id}: "
                f"{state_population!r} vs {population_of(learner_id)!r}); refusing to "
                "combine them. Re-run `python -m knowledge_engine estimate` on this "
                "data directory."
            )
    else:
        states = _empty_knowledge()

    kc_catalog = load_kc_catalog(data_dir)
    kc_topic_map, topic_map_version = load_kc_topic_map(data_dir)
    state_versions = (
        states["mapping_version"].dropna().unique()
        if "mapping_version" in states
        else []
    )
    mapping_version = (
        str(state_versions[0])
        if len(state_versions) == 1
        else topic_map_version or UNVERSIONED_MAPPING
    )
    kc_skill_map = ensure_kc_coverage(
        states,
        build_kc_skill_map(kc_catalog, kc_topic_map, mapping_version),
        mapping_version,
    )
    kc_names = dict(
        zip(kc_catalog["kc_id"].astype(str), kc_catalog["kc_name"].astype(str))
    )
    if not states.empty:
        states.insert(
            states.columns.get_loc("kc_id") + 1,
            "kc_name",
            [kc_names.get(kc_id, kc_id) for kc_id in states["kc_id"].astype(str)],
        )
    dimensions = summarize_dimensions(states)
    skill_summary = summarize_skills(states, kc_skill_map, mapping_version)
    weak_areas = identify_weak_areas(states, generated_at, mapping_version, kc_names)

    # ---- behavior, engagement, history (from the collector exports)
    completion_tracking_available = not completions.empty
    # Apply one collector cutoff to every time-dependent component. The
    # collector pipeline's established strict-before-T policy is preserved.
    events_snapshot, quizzes_snapshot, completions_snapshot, cutoff = truncate_frames(
        events, quizzes, completions, as_of_unix
    )
    learner_ids = _active_roster_ids(roster, as_of_unix)
    for frame in (events_snapshot, quizzes_snapshot, completions_snapshot):
        if frame is not None and not frame.empty and "learner_id" in frame.columns:
            learner_ids.update(frame["learner_id"].dropna().astype(str))
    learner_ids.update(states.attrs["source_learner_ids"])
    latest = _latest_timestamp_map(
        [
            _time_column(events_snapshot, "timestamp_unix", "timestamp"),
            _time_column(quizzes_snapshot, "finish_time_unix", "finish_time"),
            _time_column(completions_snapshot, "time_modified_unix", "time_modified"),
            (states, "last_attempt_at", False),
        ]
    )
    behavior = build_behavior(events_snapshot, activities, generated_at)
    engagement = build_engagement(
        events_snapshot,
        quizzes_snapshot,
        completions_snapshot,
        activities,
        generated_at,
        as_of=cutoff if cutoff is not None else generated_at,
        completion_tracking_available=completion_tracking_available,
    )
    history = build_history(
        events_snapshot,
        quizzes_snapshot,
        completions_snapshot,
        activities,
        generated_at,
    )

    activity_state, quiz_status_disagreements = build_activity_state(
        events_snapshot, quizzes_snapshot, completions_snapshot, activities
    )
    quiz_history = build_quiz_attempt_history(quizzes_snapshot, activities)

    outputs = {
        "learner_kc_state": states,
        "learner_skill_summary": skill_summary,
        "learner_dimension_summary": dimensions,
        "learner_behavior": behavior,
        "learner_engagement": engagement,
        "learner_history": history,
        "learner_activity_state": activity_state,
        "learner_quiz_attempts": quiz_history,
        "weak_areas": weak_areas,
    }
    outputs = {
        name: _with_population(frame, population_of) for name, frame in outputs.items()
    }
    profiles = build_profiles(
        population_of,
        learner_ids,
        {
            "knowledge": outputs["learner_kc_state"],
            "skills": outputs["learner_skill_summary"],
            "weak_areas": outputs["weak_areas"],
            "behavior": outputs["learner_behavior"],
            "engagement": outputs["learner_engagement"],
            "history": outputs["learner_history"],
        },
        generated_at,
        as_of=as_of_iso,
        latest_timestamps=latest,
    )
    outputs = {"learner_profile": profiles, **outputs}
    checks = validate_outputs(outputs, as_of_iso)

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in outputs.items():
        frame.to_csv(output_dir / f"{name}.csv", index=False)
    kc_skill_map.to_csv(output_dir / "kc_skill_map.csv", index=False)

    input_files = {
        name: data_dir / filename
        for name, filename in {**COLLECTOR_FILES, **OPTIONAL_INPUT_FILES}.items()
        if (data_dir / filename).is_file()
    }
    if knowledge_available:
        input_files["knowledge_state_file"] = knowledge_state_file
        engine_manifest = knowledge_state_file.with_name(
            "knowledge_state_manifest.json"
        )
        if engine_manifest.is_file():
            input_files["knowledge_state_manifest"] = engine_manifest
    components = ["behavior", "engagement", "history"]
    if knowledge_available:
        components = ["knowledge", "skills", "weak_areas", *components]
    population_counts = sorted(
        (str(population), int(count))
        for population, count in profiles["source_population"].value_counts().items()
    )
    manifest = {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of": as_of_iso,
        # One population, or "mixed"; each row carries its learner's own label.
        "source_population": (
            population_counts[0][0] if len(population_counts) == 1 else MIXED_COHORT
        )
        if population_counts
        else default_population,
        "population_counts": dict(population_counts),
        "learner_count": len(profiles),
        "components": components,
        "inputs": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in sorted(input_files.items())
        },
        "knowledge_provenance": (
            {
                "model_names": sorted(states["model_name"].dropna().unique().tolist()),
                "model_versions": sorted(
                    states["model_version"].dropna().unique().tolist()
                ),
                "estimator_phases": sorted(
                    int(v) for v in states["estimator_phase"].dropna().unique()
                ),
                "checkpoint_ids": sorted(
                    states["checkpoint_id"].dropna().unique().tolist()
                ),
                "knowledge_snapshot_as_of": states.attrs["knowledge_snapshot_as_of"],
                "state_status_counts": _counts(states["state_status"]),
                "dropped_after_cutoff": states.attrs["dropped_after_cutoff"],
                "dropped_without_last_attempt_at": states.attrs[
                    "dropped_without_timestamp"
                ],
            }
            if knowledge_available
            else None
        ),
        "weak_area_counts": _counts(weak_areas["concern_status"]),
        "activity_state": {
            "status_counts": _counts(activity_state["status"]),
            "status_source_counts": _counts(activity_state["status_source"]),
            # Quiz rows where Moodle's completion record and the outcome
            # rebuilt from the attempts differ; Moodle's record is kept.
            "quiz_status_disagreements": int(quiz_status_disagreements),
            "quizzes_without_pass_mark": bool(
                "pass_fraction" not in activities.columns
                or pd.to_numeric(
                    activities.loc[
                        activities["activity_type"].astype(str).eq("quiz"),
                        "pass_fraction",
                    ],
                    errors="coerce",
                )
                .isna()
                .any()
            ),
        },
        "mapping": {
            "version": mapping_version,
            "kc_count": int(kc_catalog["kc_id"].nunique()),
            "skill_count": int(kc_skill_map["skill_id"].nunique()),
            "status_counts": _counts(kc_skill_map["mapping_status"]),
        },
        "knowledge_semantics": {
            "predicted_correctness": "probability of answering the next question on the KC correctly",
            "p_mastery": "BKT latent probability that the KC is known; null for other estimators",
            "cold_start": "prior for a KC with no answers; not individual evidence or mastery",
        },
        "configuration": {
            "low_predicted_correctness_threshold": LOW_PREDICTED_CORRECTNESS,
            "high_predicted_correctness_threshold_reserved_unused": HIGH_PREDICTED_CORRECTNESS,
            "minimum_attempts_for_evidence_concern": MIN_ATTEMPTS_FOR_EVIDENCE_CONCERN,
            "low_observed_accuracy_threshold": LOW_OBSERVED_ACCURACY,
        },
        "output_counts": {name: len(frame) for name, frame in outputs.items()},
        "validation_checks": checks,
    }
    (output_dir / "build_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_validation_report(
        output_dir / "validation_report.md", outputs, manifest, kc_skill_map, checks
    )
    return outputs


def _write_validation_report(path, outputs, manifest, kc_skill_map, checks):
    profiles = outputs["learner_profile"]
    population = manifest["source_population"]
    lines = [
        "# Learner-model validation report",
        "",
        f"- Generated at: `{manifest['generated_at']}`",
        f"- Snapshot cutoff: `{manifest['as_of'] or 'latest'}`",
        f"- Source population: `{population}`"
        + (
            " ("
            + ", ".join(
                f"{count} {name}"
                for name, count in manifest["population_counts"].items()
            )
            + " learners)"
            if manifest["population_counts"]
            else ""
        ),
        *(
            [
                "- SIMULATED learners are a pipeline demonstration, not evidence about "
                "real learners. Each row's `source_population` says which a learner is."
            ]
            if SIMULATED_POPULATION in manifest["population_counts"]
            else []
        ),
        f"- Mapping version: `{manifest['mapping']['version']}`",
        "- `predicted_correctness` is an answer-correctness estimate, not a mastery probability.",
        "",
        "## Output record counts",
        "",
        "| Output | Rows | Null cells |",
        "|---|---:|---:|",
        *[
            f"| `{name}.csv` | {len(frame)} | {int(frame.isna().sum().sum())} |"
            for name, frame in outputs.items()
        ],
        "",
        "## Component availability",
        "",
        "| Component | Status | Learners |",
        "|---|---|---:|",
    ]
    for column in [c for c in PROFILE_COLUMNS if c.endswith("_status")]:
        for status, count in profiles[column].value_counts(dropna=False).items():
            lines.append(
                f"| {column.removesuffix('_status')} | {status} | {int(count)} |"
            )
    provenance = manifest["knowledge_provenance"]
    lines += ["", "## Knowledge"]
    if provenance is None:
        lines += [
            "",
            "No knowledge-state file was supplied: knowledge, skills and weak areas are "
            "`unavailable`. Run `python -m knowledge_engine estimate` first.",
        ]
    else:
        lines += [
            "",
            f"- Estimator: `{', '.join(provenance['model_names'])}` "
            f"(phase {', '.join(map(str, provenance['estimator_phases']))})",
            "",
            "| KC state status | Rows |",
            "|---|---:|",
            *[f"| `{k}` | {v} |" for k, v in provenance["state_status_counts"].items()],
            "",
            "| Concern status | Rows |",
            "|---|---:|",
            *[f"| `{k}` | {v} |" for k, v in manifest["weak_area_counts"].items()],
        ]
    lines += [
        "",
        "## Mapping coverage",
        "",
        "| Mapping status | KC-skill rows |",
        "|---|---:|",
        *[f"| `{k}` | {v} |" for k, v in manifest["mapping"]["status_counts"].items()],
        "",
        "## Checks",
        "",
        *[f"- PASS: {check}" for check in checks],
        "- Validation failures: none (the builder stops before writing outputs if a check fails).",
        "",
        "## Reading these outputs",
        "",
        "- A `cold_start` KC row is a prior shared by every learner without answers on that KC.",
        "- Phase 1 estimates use fixed, uncalibrated BKT parameters.",
        "- Weak-area thresholds are configuration choices, not empirically calibrated cutoffs.",
        "- Weak-area rows cover every learner-KC state; filter on `concern_status` to list concerns.",
        "- KC cutoffs are inclusive; collector cutoffs are strictly before the cutoff.",
        "- `learner_activity_state.csv` has a row only for activities a learner touched; "
        "a quiz's outcome is Moodle's completion record when it lies inside the snapshot, "
        "otherwise rebuilt from the attempts (`status_source`). "
        f"Disagreements between the two: {manifest['activity_state']['quiz_status_disagreements']}."
        + (
            " Some quizzes have no pass mark in `activity_catalog.csv`, so their outcome "
            "cannot be rebuilt from attempts (re-run the collector)."
            if manifest["activity_state"]["quizzes_without_pass_mark"]
            else ""
        ),
        "- Behavior, engagement and history describe participation, not knowledge.",
        "",
    ]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    from course_workspace import add_course_arguments, workspace_from_args

    parser = argparse.ArgumentParser(
        description="Build learner profiles from Moodle collector data and knowledge states."
    )
    add_course_arguments(parser)
    parser.add_argument("--data-dir", type=Path, help="default: the course's data/")
    parser.add_argument(
        "--knowledge-states",
        type=Path,
        help="knowledge-engine learner_kc_state.csv (default: the course's; "
        "optional: profiles build without it)",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="default: the course's learner_model/"
    )
    parser.add_argument(
        "--as-of",
        help="snapshot cutoff: Unix seconds or timezone-aware ISO datetime",
    )
    parser.add_argument(
        "--generated-at",
        help="pin the generation timestamp (ISO) for byte-identical rebuilds",
    )
    args = parser.parse_args(argv)
    if None in (args.data_dir, args.knowledge_states, args.output_dir):
        workspace = workspace_from_args(args)
        args.data_dir = args.data_dir or workspace.data
        args.knowledge_states = args.knowledge_states or workspace.knowledge_state_file
        args.output_dir = args.output_dir or workspace.learner_model
    outputs = build_complete_learner_model(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        knowledge_state_file=args.knowledge_states,
        as_of=args.as_of,
        generated_at=args.generated_at,
    )
    print(f"Learner model written to {args.output_dir}")
    for name, frame in outputs.items():
        print(f"  {name}: {len(frame)} rows")


if __name__ == "__main__":
    main()
