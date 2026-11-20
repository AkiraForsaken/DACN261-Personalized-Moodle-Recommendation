"""Move the artifacts of the pre-workspace layout into one course's workspace.

Before course workspaces, a single course's artifacts were spread over
project-level folders (``data/``, ``knowledge_engine/artifacts/``,
``data/processed/moodle_kt``, ``knowledge_tracing/artifacts/moodle_kt``, ...).
``plan()`` lists what belongs to the course, ``apply()`` moves it, including
the trained deep model, so no retraining is needed. Paths recorded inside the
model files are rewritten to the new, workspace-relative form.

Nothing of the benchmark experiment (``english_kt``) is touched.
"""

import json
import shutil
from pathlib import Path

import pandas as pd

from . import PROJECT_ROOT

COURSE_DATASET = "moodle_kt"
# Collector outputs and simulation records that sat directly in data/.
DATA_FILES = (
    "activity_catalog.csv",
    "course_modules_completion.csv",
    "kc_activity_map.csv",
    "kc_catalog.csv",
    "kc_topic_map.csv",
    "learner_roster.csv",
    "learning_events.csv",
    "learning_sessions.csv",
    "moodle_question_kc_map.csv",
    "question_answer_events.csv",
    "question_answer_mapping_report.csv",
    "quiz_attempts.csv",
    "simulation_manifest.json",
    "simulation_plan_manifest.json",
)
OLD_CHECKPOINT_PREFIX = "knowledge_tracing/artifacts/moodle_kt/"


def _course_of_export(data_dir):
    roster = Path(data_dir) / "learner_roster.csv"
    if not roster.is_file():
        return None
    ids = pd.read_csv(roster, usecols=["course_id"])["course_id"].dropna().unique()
    return int(ids[0]) if len(ids) == 1 else None


def plan(workspace, project_root=PROJECT_ROOT):
    """``[(source, destination)]`` of everything that exists and belongs to the course."""
    root = Path(project_root)
    data = root / "data"
    exported = _course_of_export(data)
    if exported is not None and exported != workspace.course_id:
        raise ValueError(
            f"{data} holds an export of course {exported}, not {workspace.course_id}; "
            f"migrate with --course {exported}"
        )
    moves = [(data / name, workspace.data / name) for name in DATA_FILES]
    moves += [(path, workspace.data / path.name) for path in sorted(data.glob("plan_*.json"))]
    moves += [
        (root / "knowledge_engine/artifacts/active_model.json", workspace.active_model_file),
        (root / "knowledge_engine/artifacts/knowledge_states", workspace.knowledge_states),
        (data / "processed" / COURSE_DATASET, workspace.kt / "processed"),
        (data / "processed/pykt_moodle", workspace.kt / "pykt"),
        (root / "knowledge_tracing/artifacts" / COURSE_DATASET, workspace.kt / "artifacts"),
        (root / "learner_model/artifacts/learner_model", workspace.learner_model),
        (root / "recommender/artifacts/recommendations", workspace.recommendations),
        (root / "recommender/artifacts/evaluation", workspace.evaluation),
        (root / "moodle_sync/artifacts/bundle", workspace.bundle),
    ]
    existing = [(source, target) for source, target in moves if source.exists()]
    clashes = [str(target) for _, target in existing if target.exists()]
    if clashes:
        raise ValueError(f"The workspace already has: {clashes[:5]}; nothing was moved")
    return existing


def _rewrite_checkpoint(value):
    """Old project-relative checkpoint path -> path relative to the course's kt/."""
    if isinstance(value, str) and value.startswith(OLD_CHECKPOINT_PREFIX):
        return "artifacts/" + value[len(OLD_CHECKPOINT_PREFIX):]
    return value


def _rewrite_json(path, update):
    if not path.is_file():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    update(record)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return True


def _move_data_config(workspace, project_root):
    """The course entry of the tracked benchmark config -> the course's own config."""
    tracked = Path(project_root) / "knowledge_tracing/configs/data_config.json"
    if not tracked.is_file():
        return False
    configs = json.loads(tracked.read_text(encoding="utf-8"))
    entry = configs.pop(COURSE_DATASET, None)
    if entry is None:
        return False
    entry = {**entry, "dpath": "pykt", "mapping_dir": "processed"}
    target = workspace.kt / "data_config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({COURSE_DATASET: entry}, indent=2), encoding="utf-8")
    tracked.write_text(json.dumps(configs, indent=2), encoding="utf-8")
    return True


def apply(workspace, project_root=PROJECT_ROOT, log=print):
    """Move everything ``plan`` lists and fix the paths recorded in model files."""
    moves = plan(workspace, project_root)
    for source, target in moves:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        log(f"moved {source} -> {target}")
    if _move_data_config(workspace, project_root):
        log(f"moved the {COURSE_DATASET} entry of configs/data_config.json -> {workspace.kt / 'data_config.json'}")

    def fix_selection(record):
        record["checkpoint"] = _rewrite_checkpoint(record.get("checkpoint"))

    def fix_decision(record):
        if isinstance(record.get("deep_model"), dict):
            fix_selection(record["deep_model"])
        if record.get("training_data_dir"):
            record["training_data_dir"] = str(workspace.data)

    if _rewrite_json(workspace.kt / "artifacts/selected_model.json", fix_selection):
        log("rewrote the checkpoint path in kt/artifacts/selected_model.json")
    if _rewrite_json(workspace.active_model_file, fix_decision):
        log("rewrote the paths in knowledge/active_model.json")
    workspace.write_course_info(migrated_from="project-level folders")
    return moves
