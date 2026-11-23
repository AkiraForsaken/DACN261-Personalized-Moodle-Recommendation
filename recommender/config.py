"""Paths and tunable settings of the recommender.

Every number below is a design choice. None is calibrated on learner data, and
the thesis must not present them as empirically optimised.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Inputs and outputs live in the course workspace (see course_workspace):
# course structure from the collector export (data/), learner state from the
# learner-model build made from that same export (learner_model/), output in
# recommendations/ and evaluation/.

RECOMMENDATIONS_FILE = "recommendations.csv"
MANIFEST_FILE = "recommendation_manifest.json"

# Bump when rules, ranking or reason texts change; stored on every row.
RECOMMENDER_VERSION = "1.3.0"

# --- list shape --------------------------------------------------------------
TOP_N = 5  # recommendations per learner
# At most this many remediation items (revise / retry / review / practise), so
# a struggling learner always keeps at least one way forward in the list.
MAX_REMEDIATION = 3
# At most this many lessons recommended on a model prediction alone.
MAX_PREPARE = 2

# --- thresholds --------------------------------------------------------------
# A KC of a failed quiz counts as weak below this observed accuracy. The
# learner model's own threshold (build_manifest.json) is used when present, so
# the recommender and the weak-area screen never disagree; this is the fallback.
LOW_OBSERVED_ACCURACY = 0.60

# --- failing a quiz ------------------------------------------------------------
# Course-design assumption, not learned from data: a KC's prerequisites are the
# KCs of the same dimension taught in earlier units. Used only after a learner
# revised a quiz's lessons and failed it again (``revisit_prerequisite``).
PREREQUISITE_RELATION = "same_dimension_earlier_unit"
# At most this many earlier-unit lessons per failed quiz.
MAX_PREREQUISITES = 2
# Quizzes allow unlimited attempts. After this many failed attempts of the
# same check quiz in a row, the recommender stops sending the learner back to
# it (no revise/retry), offers the earlier-unit lessons once and lets the
# learner move on; the teacher sees the quiz as "stuck" in the evidence. The
# quiz is offered again once the learner passes another check quiz.
MAX_FAILED_CHECK_ATTEMPTS = 3

# --- challenge (layout kc: one challenge quiz per KC) ----------------------------
# A KC counts as strong enough for harder practice with at least this many
# answers at this observed accuracy, and its check quiz passed. The accuracy
# matches the learner model's "strong" level (learner_model.config).
CHALLENGE_MIN_ANSWERS = 5
CHALLENGE_MIN_ACCURACY = 0.80
# At most this many challenge items per list; they compete for forward slots.
MAX_CHALLENGE = 1
