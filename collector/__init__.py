"""
Moodle data collector: exports one Moodle course to CSV with read-only SQL.

Entry points: run_collection(cursor, ...) for a caller-supplied cursor (tests,
notebooks); main() for the full connect -> collect -> summarise run.

Outputs, design notes, configuration and the module map: see README.md.
"""

import argparse
import json
import time
from pathlib import Path

from .collect_activities import collect_activity_catalog
from .collect_completion import collect_course_modules_completion
from .collect_course_mappings import collect_course_mappings, kc_of_module
from .collect_events import collect_learning_events
from .collect_quizzes import collect_quiz_attempts
from .collect_question_answers import (
    collect_learner_roster,
    collect_question_answer_events,
)
from .config import (
    COURSE_ID_ENV,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    INCLUDE_ROLES,
    LOCAL_TZ_NAME,
    MASTERY_THRESHOLD,
    OUTPUT_DIR_ENV,
    SESSION_GAP_SECONDS,
)
from .db import choose_course, resolve_participants, set_output_dir

__all__ = ["run_collection", "main", "COLLECTION_MANIFEST"]

# Written last by every run: which course, when, and for which learners.
COLLECTION_MANIFEST = "collection_manifest.json"


def _narrow(user_filter, learners):
    """The role filter restricted to ``learners`` (None = everyone allowed)."""
    if learners is None:
        return user_filter
    wanted = {int(learner) for learner in learners}
    return wanted if user_filter is None else set(user_filter) & wanted


def run_collection(cursor, course_id=None, output_dir=None, learners=None):
    """
    Run every collection step, in dependency order, on an open cursor.

    Writes all CSVs to ``output_dir`` and returns ``(course_id, fullname,
    shortname)``. ``learners`` limits every per-learner export (roster,
    events, attempts, answers, completion) to those Moodle user ids; course
    structure (activities, KC catalog and maps) is always exported in full.
    ``collected_at`` in the manifest is taken *before* the first query, so
    anything a learner does during the run is newer than the export.
    Contains no connection handling, so it can be driven by a fake cursor in
    tests.
    """
    collected_at = int(time.time())
    if output_dir is None:
        raise ValueError("run_collection needs an output_dir")
    directory = set_output_dir(output_dir)
    course_id, fullname, shortname = choose_course(cursor, course_id)

    print("\nResolving participants...")
    user_filter, _ = resolve_participants(cursor, course_id)
    user_filter = _narrow(user_filter, learners)
    if learners is not None:
        print(f"Learner filter: exporting learners {sorted(user_filter)} only")

    print("\nCollecting enrolled learner roster...")
    collect_learner_roster(cursor, course_id, user_filter)

    print("\nCollecting activity catalog...")
    catalog_rows = collect_activity_catalog(cursor, course_id)

    # The KC mappings come first so every logstore event can carry its KC.
    print("\nCollecting course KC mappings...")
    *_, kc_activity_map = collect_course_mappings(cursor, course_id, catalog_rows)

    print("\nCollecting learning events...")
    collect_learning_events(
        cursor, course_id, catalog_rows, user_filter, kc_of_module(kc_activity_map)
    )

    print("\nCollecting quiz attempts...")
    collect_quiz_attempts(cursor, course_id, catalog_rows, user_filter)

    print("\nCollecting scored question-answer events...")
    collect_question_answer_events(cursor, course_id, user_filter)

    print("\nCollecting activity completion...")
    collect_course_modules_completion(cursor, course_id, catalog_rows, user_filter)

    (directory / COLLECTION_MANIFEST).write_text(
        json.dumps(
            {
                "course_id": int(course_id),
                "course_fullname": fullname,
                "course_shortname": shortname,
                "collected_at": collected_at,
                "learners": None if learners is None else sorted(int(x) for x in learners),
                "role_filter": INCLUDE_ROLES,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return int(course_id), fullname, shortname


def connect():
    """An open MariaDB connection with the MOODLE_* settings."""
    if not DB_PASSWORD:
        raise RuntimeError("MOODLE_DOCKER_DBPASS is not set.")
    # Imported here (not at module top) so the rest of the package - and its
    # tests - can be imported on machines without the MariaDB driver installed.
    import mariadb

    return mariadb.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def collect(course_id=None, site=None, output_dir=None, learners=None):
    """Connect, collect one course into its workspace (or ``output_dir``), close.

    Returns ``(course_id, output_dir)``.
    """
    from course_workspace import CourseWorkspace

    print("Connecting to Moodle MariaDB...")
    conn = connect()
    cursor = conn.cursor()
    try:
        if output_dir is None and not OUTPUT_DIR_ENV:
            # The folder depends on the course: resolve the course first.
            course_id = choose_course(cursor, course_id or COURSE_ID_ENV)[0]
            workspace = CourseWorkspace.of(course_id, site)
            output_dir = workspace.data
        else:
            workspace = None
            output_dir = Path(output_dir or OUTPUT_DIR_ENV)
        course_id, fullname, shortname = run_collection(
            cursor, course_id or COURSE_ID_ENV, output_dir, learners
        )
        if workspace is not None:
            workspace.write_course_info(fullname=fullname, shortname=shortname)

        print("\nCollection complete.")
        print(f"Course: {fullname} ({shortname}), id {course_id}")
        print(f"Output: {output_dir}")
        print(f"Timezone: {LOCAL_TZ_NAME}")
        print(f"Mastery threshold: {MASTERY_THRESHOLD:.0%}")
        print(f"Session gap: {SESSION_GAP_SECONDS}s")
        print(f"Role filter: {INCLUDE_ROLES}")
    finally:
        cursor.close()
        conn.close()
    return course_id, Path(output_dir)


def _learner_list(text):
    return [int(part) for part in text.split(",") if part.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python moodle-collector.py",
        description="Export one Moodle course to CSV (read-only SQL).",
    )
    parser.add_argument(
        "--course", help="Moodle course id (default: $MOODLE_COURSE_ID, else the first visible course)"
    )
    parser.add_argument("--site", help="workspace site name (default: $MICROLEARNING_SITE or 'local')")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="write here instead of the course workspace's data/ folder",
    )
    parser.add_argument(
        "--learners",
        type=_learner_list,
        help="comma-separated Moodle user ids: export only these learners (course structure in full)",
    )
    args = parser.parse_args(argv)
    collect(args.course, args.site, args.output_dir, args.learners)
