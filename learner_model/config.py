"""Configuration for the learner-model build."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Inputs and outputs live in the course workspace (see course_workspace): the
# collector export (data/), the knowledge engine's learner_kc_state.csv (the
# only knowledge input; no benchmark dataset or benchmark learner state is
# ever read here), and this package's output (learner_model/).

KC_CATALOG_FILE = "kc_catalog.csv"
KC_TOPIC_MAP_FILE = "kc_topic_map.csv"
ROSTER_FILE = "learner_roster.csv"
# Present when the data directory contains simulated learners. Which learner
# is simulated is the roster's `is_simulated` column; the manifest alone only
# decides the label of learners the roster does not label.
SIMULATION_MANIFEST_NAME = "simulation_manifest.json"
MOODLE_POPULATION = "moodle_collector"
SIMULATED_POPULATION = "simulated"
# Label of a cohort (never of a learner) that holds both populations.
MIXED_COHORT = "mixed"

PROFILE_SCHEMA_VERSION = "2.0.0"
UNVERSIONED_MAPPING = "unversioned"

# These are auditable screening thresholds, not calibrated mastery cutoffs.
LOW_PREDICTED_CORRECTNESS = 0.60
# Reserved for future advancement/ranking use. It is deliberately
# NOT used by weak-area classification: a high prediction is not verified mastery.
HIGH_PREDICTED_CORRECTNESS = 0.80
MIN_ATTEMPTS_FOR_EVIDENCE_CONCERN = 3
LOW_OBSERVED_ACCURACY = 0.60
# Knowledge levels (knowledge_levels.py) for the teacher's charts: with at
# least MIN_ATTEMPTS_FOR_EVIDENCE_CONCERN answers, accuracy at or above this is
# "strong", between LOW_OBSERVED_ACCURACY and this "developing". Display
# categories over observed answers, not calibrated mastery cutoffs.
STRONG_OBSERVED_ACCURACY = 0.80
