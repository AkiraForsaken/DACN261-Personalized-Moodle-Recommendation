"""The shared test cases of the recommendation window (Python and the Moodle block).

``recommender.ranking.window`` and ``local_microlearning``'s
``recommendation_window::select(effective_queue(...))`` must pick the same
items. A queue item may carry ``trigger`` (a contingency row: shown only once
that quiz is failed); a case's ``active`` lists the failed quizzes. Both test
suites read tests/fixtures/recommendation_window.json. Regenerate it after changing the
slot rules:

    python -m tests.window_fixture
"""

import json
import random
from pathlib import Path

from recommender import config, schemas
from recommender.ranking import window
from recommender.rules import Candidate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "recommendation_window.json"
# The Moodle plugin's copy (its PHPUnit test cannot read the main repository).
PLUGIN_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "moodle/public/local/microlearning/tests/fixtures/recommendation_window.json"
)
CODE_OF_GROUP = {
    schemas.GROUP_REMEDIATION: schemas.REVIEW_WEAK_KC,
    schemas.GROUP_PREPARE: schemas.PREPARE_PREDICTED_DIFFICULTY,
    schemas.GROUP_FORWARD: schemas.CONTINUE_COURSE,
}


def candidates(queue):
    return [
        Candidate(item["cmid"], schemas.REVISE_BEFORE_RETRY, trigger_module_id=item["trigger"])
        if item.get("trigger")
        else Candidate(item["cmid"], CODE_OF_GROUP[item["group"]])
        for item in queue
    ]


def expected(case):
    chosen = window(
        candidates(case["queue"]), set(case["done"]), case["top_n"], case["max_remediation"],
        active_triggers=set(case.get("active", [])),
    )
    return [c.module_id for c in chosen]


def _branch(trigger, *cmids):
    return [{"cmid": cmid, "group": "contingency", "trigger": trigger} for cmid in cmids]


def make_cases(seed=11, count=40):
    rng = random.Random(seed)
    cases = [
        {"name": "nothing done: the head", "queue": [
            {"cmid": 1, "group": "remediation"}, {"cmid": 2, "group": "remediation"},
            {"cmid": 3, "group": "prepare"}, {"cmid": 4, "group": "forward"},
            {"cmid": 5, "group": "forward"}, {"cmid": 6, "group": "forward"}], "done": []},
        {"name": "five remediation items keep one slot for the way forward", "queue": [
            *({"cmid": i, "group": "remediation"} for i in range(1, 6)),
            {"cmid": 6, "group": "forward"}, {"cmid": 7, "group": "forward"}], "done": []},
        {"name": "everything done", "queue": [{"cmid": 1, "group": "forward"}], "done": [1]},
        {"name": "only remediation left", "queue": [
            {"cmid": 1, "group": "remediation"}, {"cmid": 2, "group": "forward"}], "done": [2]},
        {"name": "prepare never takes the last forward slot", "queue": [
            {"cmid": 1, "group": "prepare"}, {"cmid": 2, "group": "prepare"},
            {"cmid": 3, "group": "forward"}], "done": [], "top_n": 2},
        {"name": "a branch stays hidden while its quiz is not failed", "queue": [
            {"cmid": 1, "group": "forward"}, {"cmid": 2, "group": "forward"},
            *_branch(2, 11, 12, 2)], "done": [], "active": []},
        {"name": "a failed quiz: its lessons and the retry come first", "queue": [
            {"cmid": 4, "group": "forward"}, {"cmid": 5, "group": "forward"},
            {"cmid": 6, "group": "forward"}, *_branch(3, 1, 2, 3)],
            "done": [], "active": [3]},
        {"name": "the branch replaces the plain row of the same activity", "queue": [
            {"cmid": 1, "group": "forward"}, {"cmid": 3, "group": "forward"},
            {"cmid": 7, "group": "forward"}, *_branch(3, 1, 3)],
            "done": [], "active": [3]},
        {"name": "lessons reread after the fail: the retry is next", "queue": [
            {"cmid": 4, "group": "forward"}, *_branch(3, 1, 2, 3)],
            "done": [1, 2], "active": [3]},
        {"name": "two failed quizzes keep the remediation cap", "queue": [
            {"cmid": 9, "group": "forward"}, *_branch(3, 1, 2, 3), *_branch(6, 4, 5, 6)],
            "done": [], "active": [3, 6]},
    ]
    for index in range(count):
        size = rng.randint(1, 14)
        groups = rng.choices(["remediation", "prepare", "forward"], weights=[3, 1, 6], k=size)
        queue = [{"cmid": 100 + i, "group": g} for i, g in enumerate(groups)]
        # Failure branches of up to two quizzes, reusing queue activities and new ones.
        triggers = rng.sample([q["cmid"] for q in queue], min(size, rng.randint(0, 2)))
        for trigger in triggers:
            pool = [q["cmid"] for q in queue if q["cmid"] != trigger] + [200 + trigger * 3 + k for k in range(3)]
            queue += _branch(trigger, *rng.sample(pool, rng.randint(1, 3)), trigger)
        active = sorted(t for t in triggers if rng.random() < 0.6)
        cmids = sorted({q["cmid"] for q in queue})
        done = sorted(rng.sample(cmids, rng.randint(0, len(cmids))))
        cases.append({"name": f"random {index}", "queue": queue, "done": done,
                      "active": active, "top_n": rng.choice([1, 3, 5])})
    for case in cases:
        case.setdefault("top_n", config.TOP_N)
        case.setdefault("max_remediation", config.MAX_REMEDIATION)
        case.setdefault("active", [])
        case["expected"] = expected(case)
    return cases


def write(paths=(FIXTURE, PLUGIN_FIXTURE)):
    text = json.dumps({"cases": make_cases()}, indent=1) + "\n"
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return paths


if __name__ == "__main__":
    for path in write():
        print(f"wrote {path}")
