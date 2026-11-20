"""Non-personalised comparison policies for the evaluation.

Each baseline answers "what would we recommend without the learner model?" and
returns candidates in rank order, like the rules. All of them skip activities
the learner has already completed, so the comparison is about *which* open
activity is chosen, not about recommending finished work.

    course_order  the first open activities of the course, from the top. It
                  ignores where the learner currently is.
    popularity    the open activities most learners in the snapshot have
                  completed.
    random        open activities drawn uniformly (seeded per learner).
"""

import hashlib
import random
from collections import Counter

from . import schemas
from .rules import Candidate


def _open(learner, course):
    return [a for a in course.activities if not learner.is_done(a.module_id)]


def course_order(learner, course, top_n, **_):
    return [
        Candidate(a.module_id, schemas.BASELINE_COURSE_ORDER)
        for a in _open(learner, course)[:top_n]
    ]


def popularity_counts(learners):
    """module_id -> number of learners who have completed it."""
    counts = Counter()
    for learner in learners.values():
        counts.update(m for m, state in learner.activities.items() if state.done)
    return counts


def popularity(learner, course, top_n, popularity=None, **_):
    counts = popularity or {}
    ranked = sorted(
        _open(learner, course), key=lambda a: (-counts.get(a.module_id, 0), a.sequence)
    )
    return [
        Candidate(
            a.module_id,
            schemas.BASELINE_POPULARITY,
            evidence={"completed_by_learners": int(counts.get(a.module_id, 0))},
        )
        for a in ranked[:top_n]
    ]


def random_open(learner, course, top_n, seed=0, **_):
    # A stable per-learner stream: the same snapshot always gives the same list.
    digest = hashlib.sha256(f"{seed}:{learner.learner_id}".encode()).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    pool = _open(learner, course)
    return [
        Candidate(a.module_id, schemas.BASELINE_RANDOM)
        for a in rng.sample(pool, min(top_n, len(pool)))
    ]
