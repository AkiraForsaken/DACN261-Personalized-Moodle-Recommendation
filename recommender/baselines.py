"""Non-personalised comparison policies for the evaluation.

Each baseline answers "what would we recommend without the learner model?" and
returns candidates in rank order, like the rules. All of them skip activities
the learner has already completed, so the comparison is about *which* open
activity is chosen, not about recommending finished work.

    course_order  the first open activities of the course, from the top. It
                  ignores where the learner currently is.
    popularity    the open activities most learners of the same population
                  in the snapshot have completed.
    random        open activities drawn uniformly (seeded per learner).

Two personalised comparison policies use the learner's data, but none of the
recommender's rules, thresholds or slots. They exist so that the rules can be
compared with the standard families of the recommender literature:

    content_based  open activities whose content (KC dimension and unit) is
                   most similar to the content the learner has got wrong.
    item_cf        item-based collaborative filtering: open activities that
                   learners of the same population touched together with the
                   activities this learner touched.

Both fall back explicitly when the learner gives them nothing to work with
(content_based: no answers -> course order; item_cf: nothing touched ->
popularity), and the evidence column says which happened.
"""

import hashlib
import math
import random
from collections import Counter, defaultdict

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
    """population -> {module_id: number of learners who have completed it}.

    Counted per learner population, like ``item_similarities``, so real and
    simulated learners never inform each other's lists.
    """
    counts = defaultdict(Counter)
    for learner in learners.values():
        counts[learner.population].update(
            m for m, state in learner.activities.items() if state.done
        )
    return dict(counts)


def popularity(learner, course, top_n, popularity=None, **_):
    """Open activities most completed by learners of the same population."""
    counts = (popularity or {}).get(learner.population) or {}
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


# ------------------------------------------------------------- content-based
def _kcs_of_activity(course, module_id):
    """The KCs an activity is about: a lesson's KC, or a quiz's KCs."""
    kc_id = course.kc_of_lesson.get(module_id)
    if kc_id is not None:
        return [kc_id]
    return list(course.kcs_of_quiz.get(module_id, []))


def _content_features(course, kc_ids):
    """Bag of content features of a set of KCs: its dimensions and its units."""
    features = Counter()
    for kc_id in kc_ids:
        dimension = course.kc_dimension.get(kc_id)
        unit = course.unit_of_kc(kc_id)
        if dimension:
            features[f"dimension:{dimension}"] += 1
        if unit is not None:
            features[f"unit:{unit}"] += 1
    return features


def _cosine(a, b):
    dot = sum(value * b.get(key, 0.0) for key, value in a.items())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def content_profile(learner, course):
    """What the learner needs, as content features weighted by error rate.

    Every KC the learner has answered contributes its dimension and unit with
    weight 1 - observed accuracy, so content the learner gets wrong dominates.
    Empty when the learner has no answers.
    """
    profile = Counter()
    for kc_id, state in learner.kcs.items():
        if not state.attempts or state.observed_accuracy is None:
            continue
        need = 1.0 - float(state.observed_accuracy)
        if need <= 0:
            continue
        for feature, count in _content_features(course, [kc_id]).items():
            profile[feature] += need * count
    return profile


def content_based(learner, course, top_n, **_):
    """Open activities most similar in content to what the learner gets wrong.

    Activity features are the dimensions and units of its KCs (no difficulty:
    the course records none per activity). Ties, activities without KCs and
    learners without answers fall back to course order.
    """
    profile = content_profile(learner, course)
    scored = []
    for activity in _open(learner, course):
        features = _content_features(course, _kcs_of_activity(course, activity.module_id))
        scored.append((_cosine(profile, features) if profile else 0.0, activity))
    scored.sort(key=lambda pair: (-pair[0], pair[1].sequence))
    fallback = not profile
    return [
        Candidate(
            activity.module_id,
            schemas.BASELINE_CONTENT_BASED,
            kc_id=course.kc_of_lesson.get(activity.module_id),
            evidence={
                "similarity": round(score, 4),
                "fallback": "course_order" if fallback else None,
            },
        )
        for score, activity in scored[:top_n]
    ]


# ------------------------------------------------- item-based collaborative
def item_similarities(learners):
    """Item-item cosine similarity on the binary learner x activity matrix.

    An activity counts as touched when the learner has any record on it
    (viewed, attempted or completed). Computed separately per learner
    population, so real and simulated learners never inform each other.
    Returns ``{population: {module_id: {other_module_id: similarity}}}``.
    """
    by_population = defaultdict(list)
    for learner in learners.values():
        by_population[learner.population].append(set(learner.activities))
    result = {}
    for population, baskets in by_population.items():
        counts, pairs = Counter(), Counter()
        for basket in baskets:
            items = sorted(basket)
            counts.update(items)
            for i, first in enumerate(items):
                for second in items[i + 1 :]:
                    pairs[(first, second)] += 1
        similar = defaultdict(dict)
        for (first, second), together in pairs.items():
            value = together / math.sqrt(counts[first] * counts[second])
            similar[first][second] = value
            similar[second][first] = value
        result[population] = {"similar": dict(similar), "popularity": counts}
    return result


def item_cf(learner, course, top_n, similarities=None, **_):
    """Open activities co-touched with this learner's activities by peers.

    Score of an open activity j: the sum over the learner's touched
    activities i of sim(i, j), within the learner's own population. A learner
    who has touched nothing gets the population's most-touched activities.
    """
    model = (similarities or {}).get(learner.population) or {"similar": {}, "popularity": {}}
    touched = set(learner.activities)
    scores = Counter()
    for module_id in touched:
        for other, value in model["similar"].get(module_id, {}).items():
            scores[other] += value
    fallback = not scores
    if fallback:
        scores = Counter(model["popularity"])
    ranked = sorted(
        _open(learner, course), key=lambda a: (-scores.get(a.module_id, 0), a.sequence)
    )
    return [
        Candidate(
            a.module_id,
            schemas.BASELINE_ITEM_CF,
            kc_id=course.kc_of_lesson.get(a.module_id),
            evidence={
                "score": round(float(scores.get(a.module_id, 0)), 4),
                "fallback": "popularity" if fallback else None,
            },
        )
        for a in ranked[:top_n]
    ]
