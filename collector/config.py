"""
Collector configuration: every environment-driven constant lives here.

Nothing in this module talks to the database. Values are read once at import
time, so set the MOODLE_* environment variables before importing the package.
"""

import os
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DB_HOST = os.getenv("MOODLE_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("MOODLE_DB_PORT", "3306"))
DB_NAME = os.getenv("MOODLE_DB_NAME", "moodle")
DB_USER = os.getenv("MOODLE_DB_USER", "moodle")
DB_PASSWORD = os.getenv("MOODLE_DOCKER_DBPASS")
DB_PREFIX = os.getenv("MOODLE_DB_PREFIX", "m_")

# The course is chosen per run (``--course`` or MOODLE_COURSE_ID), and so is
# the output directory: by default the course's workspace ``data/`` folder
# (see course_workspace). MOODLE_OUTPUT_DIR still overrides it.
COURSE_ID_ENV = os.getenv("MOODLE_COURSE_ID")
OUTPUT_DIR_ENV = os.getenv("MOODLE_OUTPUT_DIR")
LOCAL_TZ_NAME = os.getenv("MOODLE_TZ", "Asia/Ho_Chi_Minh")
MASTERY_THRESHOLD = float(os.getenv("MOODLE_MASTERY_THRESHOLD", "0.80"))

# Session boundary: consecutive events by the same learner that are this far
# apart (or closer) belong to the same continuous learning session.
SESSION_GAP_SECONDS = int(os.getenv("MOODLE_SESSION_GAP_SECONDS", "1800"))

# Which enrolled roles count as "learners". Set to "all" to disable filtering
# (useful when debugging, but teacher/admin authoring traffic will pollute the
# learner model; a teacher who edits the course can produce more events than
# all learners together).
INCLUDE_ROLES = os.getenv("MOODLE_INCLUDE_ROLES", "student")

# By default the question-to-KC mapping is derived from Moodle question
# categories (collect_course_mappings.py) and written to the output directory
# as QUESTION_KC_MAP_NAME. Setting MOODLE_QUESTION_KC_MAPPING points at a
# hand-curated crosswalk instead; that file is then the source of truth and is
# never rewritten.
QUESTION_KC_MAP_NAME = "moodle_question_kc_map.csv"
QUESTION_KC_MAPPING_OVERRIDE = os.getenv("MOODLE_QUESTION_KC_MAPPING")
QUESTION_KC_MAPPING_IS_OVERRIDE = bool(QUESTION_KC_MAPPING_OVERRIDE)

LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

CONTEXT_MODULE = 70

# --------------------------------------------------------------------------
# Table-name helper
# --------------------------------------------------------------------------


def t(name):
    """Prefix a Moodle table name (default prefix m_)."""
    return f"{DB_PREFIX}{name}"
