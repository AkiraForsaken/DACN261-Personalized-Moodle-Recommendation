"""Build the bundle the Moodle plugin imports.

The bundle is the contract between the Python pipeline and the PHP plugin
``local_microlearning``: a directory of flat CSV files plus ``manifest.json``.
The plugin knows these files and nothing about the pipeline's own layout.

    profiles.csv         one row per learner (profile + engagement + history)
    kc_states.csv        learner x KC: knowledge state, level, concern and reason,
                         with the KC's unit and its lesson / practice quiz
    skills.csv           learner x skill (dimension x unit) aggregates
    dimensions.csv       learner x dimension aggregates
    attempts.csv         learner x finished quiz attempt (score over time)
    recommendations.csv  learner x rank: the full queue (rank <= top_n is the
                         list before the learner does anything), then the
                         "if this quiz is failed" branches (group contingency,
                         trigger_cmid = the quiz)
    manifest.json        schema version, kind, course id, data time, list
                         settings, provenance, row counts, SHA-256 of every file

Two kinds of bundle:

    snapshot        every learner of the course; becomes the active snapshot
    learner_update  only the learners named in ``learners``; replaces their
                    rows in the active snapshot (a per-learner refresh)

Learner ids are Moodle user ids and ``cmid`` values are Moodle course-module
ids; the plugin checks both against the course before loading.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from recommender import config as recommender_config
from recommender.course import build_course_graph

BUNDLE_SCHEMA_VERSION = 3  # 3: contingency rows, trigger_cmid
KIND_SNAPSHOT = "snapshot"
KIND_LEARNER_UPDATE = "learner_update"
COLLECTION_MANIFEST = "collection_manifest.json"
LEVEL_COUNTS = [
    "strong_kc_count",
    "developing_kc_count",
    "weak_kc_count",
    "started_kc_count",
    "not_assessed_kc_count",
]

PROFILE_COLUMNS = [
    "userid",
    "population",
    "knowledge_phase",
    "knowledge_model",
    "knowledge_status",
    "engagement_status",
    "session_count",
    "learning_seconds",
    "completed_activities",
    "course_progress",
    "last_active",
    "quiz_attempts",
    "learning_days",
]
KC_STATE_COLUMNS = [
    "userid",
    "kc_id",
    "kc_name",
    "dimension",
    "topic",
    "unit_number",
    "kc_order",
    "lesson_cmid",
    "quiz_cmid",
    "attempt_count",
    "correct_count",
    "last_attempt",
    "observed_accuracy",
    "predicted_correctness",
    "state_status",
    "knowledge_level",
    "concern_status",
    "reason",
]
SKILL_COLUMNS = [
    "userid",
    "skill_id",
    "skill_name",
    "dimension",
    "unit_number",
    "kc_count",
    "estimated_kc_count",
    "mean_predicted_correctness",
    "mean_observed_accuracy",
    "attempt_count",
    *LEVEL_COUNTS,
]
DIMENSION_COLUMNS = [
    "userid",
    "dimension",
    "kc_count",
    "estimated_kc_count",
    "mean_predicted_correctness",
    "mean_observed_accuracy",
    "attempt_count",
    *LEVEL_COUNTS,
]
ATTEMPT_COLUMNS = [
    "userid",
    "cmid",
    "attempt",
    "timefinish",
    "score",
    "pass_fraction",
    "passed",
]
RECOMMENDATION_COLUMNS = [
    "userid",
    "rank",
    "group",
    "trigger_cmid",
    "cmid",
    "activity_type",
    "kc_id",
    "kc_name",
    "reason_code",
    "reason_text",
    "basis",
    "evidence",
]


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path, **kwargs):
    path = Path(path)
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"learner_id": "string"}, **kwargs)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _user_ids(frame, name):
    """Moodle user ids from ``learner_id``; a non-numeric id cannot be imported."""
    ids = pd.to_numeric(frame["learner_id"], errors="coerce")
    if ids.isna().any():
        bad = frame.loc[ids.isna(), "learner_id"].unique()[:5].tolist()
        raise ValueError(
            f"{name}: learner ids {bad} are not Moodle user ids. Only a collector "
            "export of a Moodle course can be published (not offline-simulator data)."
        )
    return ids.astype("int64")


def _unix(series):
    stamps = pd.to_datetime(series, errors="coerce", utc=True)
    seconds = (stamps - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
    return seconds.round().astype("Int64")


def _number(series, digits=None):
    values = pd.to_numeric(series, errors="coerce")
    return values.round(digits) if digits is not None else values


def _count(frame, column):
    if column not in frame:
        return pd.Series(pd.NA, index=frame.index, dtype="Int64")
    return pd.to_numeric(frame[column], errors="coerce").astype("Int64")


def _course_id(data_dir):
    roster = _read(Path(data_dir) / "learner_roster.csv")
    if roster.empty or "course_id" not in roster.columns:
        raise ValueError(
            f"{Path(data_dir) / 'learner_roster.csv'} is missing; it names the Moodle course"
        )
    course_ids = pd.to_numeric(roster["course_id"], errors="coerce").dropna().unique()
    if len(course_ids) != 1 or int(course_ids[0]) <= 1:
        raise ValueError("The learner roster must name exactly one Moodle course")
    return int(course_ids[0])


def _data_time(data_dir, build):
    """Unix seconds of the data the profiles describe.

    The collector's ``collected_at`` (taken before its first query), or the
    build's ``as_of`` cutoff when one was set. Anything a learner does after
    this moment is new to Moodle's live filter.
    """
    if build.get("as_of"):
        return int(pd.Timestamp(build["as_of"]).timestamp())
    path = Path(data_dir) / COLLECTION_MANIFEST
    if path.is_file():
        collected = json.loads(path.read_text(encoding="utf-8")).get("collected_at")
        if collected is not None:
            return int(collected)
    return int(pd.Timestamp(build["generated_at"]).timestamp())


def _course_structure(data_dir):
    """KC -> unit number, order, lesson and practice quiz, as the recommender sees them."""
    data_dir = Path(data_dir)
    catalog = _read(data_dir / "activity_catalog.csv")
    links = _read(data_dir / "kc_activity_map.csv")
    if catalog.empty:
        return {}, {}
    graph = build_course_graph(catalog, links)
    unit_of_topic = (
        catalog.assign(
            section_number=pd.to_numeric(catalog["section_number"], errors="coerce")
        )
        .dropna(subset=["topic", "section_number"])
        .groupby("topic")["section_number"]
        .min()
        .astype(int)
        .to_dict()
        if {"topic", "section_number"} <= set(catalog.columns)
        else {}
    )
    order = {}
    for activity in graph.activities:
        kc = graph.kc_of_lesson.get(activity.module_id)
        if kc is not None:
            order.setdefault(kc, len(order) + 1)
    per_kc = {
        kc: {
            "kc_order": order.get(kc),
            "lesson_cmid": graph.lesson_of_kc.get(kc),
            "quiz_cmid": graph.quiz_of_kc.get(kc),
        }
        for kc in set(graph.lesson_of_kc) | set(graph.quiz_of_kc)
    }
    return per_kc, unit_of_topic


def _profiles(profile_dir):
    profiles = _read(profile_dir / "learner_profile.csv")
    merged = profiles
    for name, columns in (
        (
            "learner_engagement.csv",
            [
                "active_session_count",
                "estimated_learning_time_seconds",
                "completed_activity_count",
                "course_progress",
                "last_active_at",
            ],
        ),
        ("learner_history.csv", ["total_quiz_attempts", "historical_learning_days"]),
    ):
        frame = _read(profile_dir / name)
        if frame.empty:
            for column in columns:
                merged[column] = pd.NA
        else:
            merged = merged.merge(
                frame[["learner_id", *columns]], on="learner_id", how="left"
            )
    return pd.DataFrame(
        {
            "userid": _user_ids(merged, "learner_profile.csv"),
            "population": merged["source_population"],
            "knowledge_phase": _count(merged, "knowledge_phase"),
            "knowledge_model": merged["knowledge_model"],
            "knowledge_status": merged["knowledge_status"],
            "engagement_status": merged["engagement_status"],
            "session_count": _count(merged, "active_session_count"),
            "learning_seconds": _count(merged, "estimated_learning_time_seconds"),
            "completed_activities": _count(merged, "completed_activity_count"),
            "course_progress": _number(merged["course_progress"], 4),
            "last_active": _unix(merged["last_active_at"]),
            "quiz_attempts": _count(merged, "total_quiz_attempts"),
            "learning_days": _count(merged, "historical_learning_days"),
        }
    )[PROFILE_COLUMNS]


def _kc_states(profile_dir, per_kc, unit_of_topic):
    states = _read(profile_dir / "learner_kc_state.csv")
    if states.empty:
        return pd.DataFrame(columns=KC_STATE_COLUMNS)
    weak = _read(profile_dir / "weak_areas.csv")
    weak = weak[weak["entity_type"].eq("kc")]
    weak_columns = ["learner_id", "entity_id", "concern_status", "reason"]
    if "knowledge_level" in weak:
        weak_columns.append("knowledge_level")
    skill_map = pd.read_csv(profile_dir / "kc_skill_map.csv", dtype=str)
    states = states.merge(
        weak[weak_columns].rename(columns={"entity_id": "kc_id"}),
        on=["learner_id", "kc_id"],
        how="left",
    ).merge(
        skill_map[["kc_id", "topic"]].drop_duplicates("kc_id"), on="kc_id", how="left"
    )
    attempts = pd.to_numeric(states["attempt_count"], errors="coerce")
    accuracy = pd.to_numeric(states["observed_accuracy"], errors="coerce")
    kc = states["kc_id"].astype(str)
    structure = lambda key: kc.map(lambda k: per_kc.get(k, {}).get(key)).astype(
        "Int64"
    )  # noqa: E731
    return pd.DataFrame(
        {
            "userid": _user_ids(states, "learner_kc_state.csv"),
            "kc_id": kc,
            "kc_name": states["kc_name"],
            "dimension": states["kc_dimension"],
            "topic": states["topic"],
            "unit_number": states["topic"].map(unit_of_topic).astype("Int64"),
            "kc_order": structure("kc_order"),
            "lesson_cmid": structure("lesson_cmid"),
            "quiz_cmid": structure("quiz_cmid"),
            "attempt_count": attempts.astype("Int64"),
            "correct_count": (attempts * accuracy).round().astype("Int64"),
            "last_attempt": _unix(states["last_attempt_at"]),
            "observed_accuracy": accuracy.round(4),
            "predicted_correctness": _number(states["predicted_correctness"], 4),
            "state_status": states["state_status"],
            "knowledge_level": states.get("knowledge_level"),
            "concern_status": states["concern_status"],
            "reason": states["reason"],
        }
    )[KC_STATE_COLUMNS]


def _skills(profile_dir, unit_of_topic):
    skills = _read(profile_dir / "learner_skill_summary.csv")
    if skills.empty:
        return pd.DataFrame(columns=SKILL_COLUMNS)
    skill_map = pd.read_csv(profile_dir / "kc_skill_map.csv", dtype=str)
    topic_of_skill = {}
    if {"skill_id", "topic"} <= set(skill_map.columns):
        known = skill_map.dropna(subset=["skill_id"]).drop_duplicates("skill_id")
        topic_of_skill = dict(zip(known["skill_id"], known["topic"]))
    return pd.DataFrame(
        {
            "userid": _user_ids(skills, "learner_skill_summary.csv"),
            "skill_id": skills["skill_id"],
            "skill_name": skills["skill_name"],
            "dimension": skills["kc_dimension"],
            "unit_number": skills["skill_id"]
            .map(topic_of_skill)
            .map(unit_of_topic)
            .astype("Int64"),
            "kc_count": _count(skills, "kc_count"),
            "estimated_kc_count": _count(skills, "estimated_kc_count"),
            "mean_predicted_correctness": _number(
                skills["mean_predicted_correctness"], 4
            ),
            "mean_observed_accuracy": _number(skills["mean_observed_accuracy"], 4),
            "attempt_count": _count(skills, "total_attempt_count"),
            **{column: _count(skills, column) for column in LEVEL_COUNTS},
        }
    )[SKILL_COLUMNS]


def _dimensions(profile_dir):
    dimensions = _read(profile_dir / "learner_dimension_summary.csv")
    if dimensions.empty:
        return pd.DataFrame(columns=DIMENSION_COLUMNS)
    return pd.DataFrame(
        {
            "userid": _user_ids(dimensions, "learner_dimension_summary.csv"),
            "dimension": dimensions["kc_dimension"],
            "kc_count": _count(dimensions, "kc_count"),
            "estimated_kc_count": _count(dimensions, "estimated_kc_count"),
            "mean_predicted_correctness": _number(
                dimensions["mean_predicted_correctness"], 4
            ),
            "mean_observed_accuracy": _number(dimensions["mean_observed_accuracy"], 4),
            "attempt_count": _count(dimensions, "total_attempt_count"),
            **{column: _count(dimensions, column) for column in LEVEL_COUNTS},
        }
    )[DIMENSION_COLUMNS]


def _attempts(profile_dir):
    attempts = _read(profile_dir / "learner_quiz_attempts.csv")
    if attempts.empty:
        return pd.DataFrame(columns=ATTEMPT_COLUMNS)
    return pd.DataFrame(
        {
            "userid": _user_ids(attempts, "learner_quiz_attempts.csv"),
            "cmid": pd.to_numeric(attempts["course_module_id"], errors="coerce").astype(
                "int64"
            ),
            "attempt": _count(attempts, "attempt"),
            "timefinish": _unix(attempts["finished_at"]),
            "score": _number(attempts["normalized_score"], 4),
            "pass_fraction": _number(attempts["pass_fraction"], 4),
            "passed": _count(attempts, "passed"),
        }
    )[ATTEMPT_COLUMNS]


def _recommendations(recommendation_dir):
    recommendations = _read(recommendation_dir / "recommendations.csv")
    if recommendations.empty:
        return pd.DataFrame(columns=RECOMMENDATION_COLUMNS)
    return pd.DataFrame(
        {
            "userid": _user_ids(recommendations, "recommendations.csv"),
            "rank": recommendations["rank"].astype("int64"),
            "group": recommendations["group"],
            # Older recommendation files have no failure branches.
            "trigger_cmid": pd.to_numeric(
                recommendations.get(
                    "trigger_cmid", pd.Series(index=recommendations.index)
                ),
                errors="coerce",
            ).astype("Int64"),
            "cmid": recommendations["course_module_id"].astype("int64"),
            "activity_type": recommendations["activity_type"],
            "kc_id": recommendations["kc_id"],
            "kc_name": recommendations["kc_name"],
            "reason_code": recommendations["reason_code"],
            "reason_text": recommendations["reason_text"],
            "basis": recommendations["basis"],
            "evidence": recommendations["evidence"],
        }
    )[RECOMMENDATION_COLUMNS]


def build_bundle(
    profile_dir, recommendation_dir, data_dir, bundle_dir, kind=KIND_SNAPSHOT
):
    """Write the bundle directory and return its manifest.

    ``kind=learner_update`` publishes only the learners of this build (a
    per-learner refresh); the plugin then replaces their rows in the active
    snapshot instead of creating a new one.
    """
    if kind not in (KIND_SNAPSHOT, KIND_LEARNER_UPDATE):
        raise ValueError(f"Unknown bundle kind {kind!r}")
    profile_dir, recommendation_dir = Path(profile_dir), Path(recommendation_dir)
    bundle_dir = Path(bundle_dir)
    build = json.loads(
        (profile_dir / "build_manifest.json").read_text(encoding="utf-8")
    )
    rec_manifest = json.loads(
        (recommendation_dir / "recommendation_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    # The recommendations must come from exactly this learner-model build.
    recorded = rec_manifest["inputs"].get("build_manifest", {}).get("sha256")
    if recorded != _sha256(profile_dir / "build_manifest.json"):
        raise ValueError(
            "The recommendations were generated from another learner-model build. "
            "Run `python -m recommender generate` again."
        )

    per_kc, unit_of_topic = _course_structure(data_dir)
    files = {
        "profiles.csv": _profiles(profile_dir),
        "kc_states.csv": _kc_states(profile_dir, per_kc, unit_of_topic),
        "skills.csv": _skills(profile_dir, unit_of_topic),
        "dimensions.csv": _dimensions(profile_dir),
        "attempts.csv": _attempts(profile_dir),
        "recommendations.csv": _recommendations(recommendation_dir),
    }
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in files.items():
        frame.to_csv(bundle_dir / name, index=False)
    provenance = build.get("knowledge_provenance") or {}
    configuration = rec_manifest.get("configuration", {})
    learners = sorted(int(user) for user in files["profiles.csv"]["userid"])
    manifest = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "course_id": _course_id(data_dir),
        # When Moodle's data was read: the live filter counts only what
        # learners do after this moment as new.
        "collected_at": _data_time(data_dir, build),
        "profiles_generated_at": build["generated_at"],
        "recommendations_generated_at": rec_manifest["generated_at"],
        "as_of": build.get("as_of"),
        "cohort": build.get("source_population"),
        "population_counts": build.get("population_counts", {}),
        "learners": learners if kind == KIND_LEARNER_UPDATE else None,
        "mapping_version": build.get("mapping", {}).get("version"),
        "profile_schema_version": build.get("profile_schema_version"),
        "recommender_version": rec_manifest["recommender_version"],
        "top_n": int(configuration.get("top_n", recommender_config.TOP_N)),
        "max_remediation": int(
            configuration.get("max_remediation", recommender_config.MAX_REMEDIATION)
        ),
        "max_prepare": int(
            configuration.get("max_prepare", recommender_config.MAX_PREPARE)
        ),
        "knowledge_models": provenance.get("model_names", []),
        "checkpoint_ids": provenance.get("checkpoint_ids", []),
        "estimator_phases": provenance.get("estimator_phases", []),
        "files": {
            name: {"rows": int(len(frame)), "sha256": _sha256(bundle_dir / name)}
            for name, frame in files.items()
        },
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
