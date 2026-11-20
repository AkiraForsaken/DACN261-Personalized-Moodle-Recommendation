"""Knowledge levels: one display category per learner and KC, from answers only.

    not_assessed  no answer on the KC
    started       1 or 2 answers: too few to judge
    weak          >= MIN_ATTEMPTS_FOR_EVIDENCE_CONCERN answers, accuracy below
                  LOW_OBSERVED_ACCURACY  (exactly the evidence-based concern)
    developing    as many answers, accuracy from LOW_ up to STRONG_OBSERVED_ACCURACY
    strong        as many answers, accuracy at or above STRONG_OBSERVED_ACCURACY

The level ignores model predictions on purpose. It means the same whether BKT
or the deep model is in charge (Phase 1 or 3), and a teacher can check it
against the learner's answers. It is a display category with uncalibrated
thresholds, not a mastery estimate.
"""

import pandas as pd

from .config import (
    LOW_OBSERVED_ACCURACY,
    MIN_ATTEMPTS_FOR_EVIDENCE_CONCERN,
    STRONG_OBSERVED_ACCURACY,
)

LEVEL_STRONG = "strong"
LEVEL_DEVELOPING = "developing"
LEVEL_WEAK = "weak"
LEVEL_STARTED = "started"
LEVEL_NOT_ASSESSED = "not_assessed"
# Display order, best first.
KNOWLEDGE_LEVELS = (
    LEVEL_STRONG,
    LEVEL_DEVELOPING,
    LEVEL_WEAK,
    LEVEL_STARTED,
    LEVEL_NOT_ASSESSED,
)
LEVEL_COUNT_COLUMNS = [f"{level}_kc_count" for level in KNOWLEDGE_LEVELS]


def knowledge_level(attempts, observed_accuracy):
    """The level of one learner-KC state from its answer count and accuracy."""
    attempts = 0 if attempts is None or pd.isna(attempts) else int(attempts)
    if attempts == 0:
        return LEVEL_NOT_ASSESSED
    if (
        attempts < MIN_ATTEMPTS_FOR_EVIDENCE_CONCERN
        or observed_accuracy is None
        or pd.isna(observed_accuracy)
    ):
        return LEVEL_STARTED
    if observed_accuracy < LOW_OBSERVED_ACCURACY:
        return LEVEL_WEAK
    if observed_accuracy < STRONG_OBSERVED_ACCURACY:
        return LEVEL_DEVELOPING
    return LEVEL_STRONG


def levels_of(states):
    """A Series with the level of every row of a learner-KC state frame."""
    if states.empty:
        return pd.Series([], dtype="object", index=states.index)
    return pd.Series(
        [
            knowledge_level(attempts, observed)
            for attempts, observed in zip(
                states["attempt_count"], pd.to_numeric(states["observed_accuracy"], errors="coerce")
            )
        ],
        index=states.index,
        dtype="object",
    )


def level_counts(states, keys):
    """Per group of ``keys``: how many KCs sit at each level (columns LEVEL_COUNT_COLUMNS)."""
    if states.empty:
        return pd.DataFrame(columns=[*keys, *LEVEL_COUNT_COLUMNS])
    levels = states[keys].assign(_level=levels_of(states))
    counts = (
        pd.crosstab([levels[key] for key in keys], levels["_level"])
        .reindex(columns=list(KNOWLEDGE_LEVELS), fill_value=0)
        .rename(columns=lambda level: f"{level}_kc_count")
        .reset_index()
    )
    counts.columns.name = None
    return counts[[*keys, *LEVEL_COUNT_COLUMNS]]
