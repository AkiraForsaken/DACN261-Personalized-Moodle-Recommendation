"""Attach the generator's probabilities and Moodle user ids to a saved plan.

A plan written by ``simulate-learners`` (before 2026-11-18) holds every answer
but not the probability it was drawn with, and not the Moodle account it was
injected into. Both are needed to compute the oracle AUC on the gate's own
targets (``knowledge_engine.oracle``). The plan is seeded, so it can be made
again: ``annotate_plan`` regenerates it with the recorded settings and the
course's current structure, **requires every learner, attempt and answer to be
identical**, then copies ``p_correct`` onto the stored answers and adds
``moodle_userid`` to each learner (account ``SIM-<number>``).

If the regeneration differs (simulator code or course changed since), nothing
is written: the probabilities would not belong to the answers in Moodle.
"""

import json

from knowledge_engine.simulator import DEFAULT_LESSON_GAIN, DEFAULT_PASS_FRACTION, simulate_plan

PLAN_KEYS = ("quiz_id", "attempt_number", "entry_id", "correct", "time")


class PlanMismatch(ValueError):
    """The regenerated plan is not the stored one."""


def _settings(manifest):
    recorded = manifest.get("parameters") or {}
    pages = manifest.get("page_behaviour") or {}
    return {
        "n_learners": recorded.get("n_learners", manifest["learners"]),
        "seed": manifest["seed"],
        "start_unix": manifest["start_unix"],
        "inactive_share": recorded.get("inactive_share", 0.05),
        "session_scale": recorded.get("session_scale", 3.5),
        "sample_range": tuple(recorded["sample_range"]) if recorded.get("sample_range") else None,
        "lesson_gain": recorded.get("lesson_gain", pages.get("lesson_gain", DEFAULT_LESSON_GAIN)),
        "pass_fraction": recorded.get(
            "pass_fraction", manifest.get("pass_fraction_default", DEFAULT_PASS_FRACTION)
        ),
    }


def _answer_keys(learners):
    return [
        (learner["number"], *(answer.get(k) for k in PLAN_KEYS[2:]),
         attempt["quiz_id"], attempt["attempt_number"])
        for learner in learners
        for attempt in learner["attempts"]
        for answer in attempt["answers"]
    ]


def _strip(learners):
    """The plan without the fields annotation adds, for comparison."""
    text = json.dumps(learners, sort_keys=True)
    plain = json.loads(text)
    for learner in plain:
        learner.pop("moodle_userid", None)
        for attempt in learner["attempts"]:
            for answer in attempt["answers"]:
                answer.pop("p_correct", None)
    return plain


def annotate_plan(stored, structure, users):
    """Return the stored plan with ``p_correct`` per answer and ``moodle_userid`` per learner.

    ``stored`` is a plan file's content (``{"manifest", "learners"}``),
    ``structure`` the course export ``simulate-learners`` used
    (``export_structure.php``), ``users`` ``{plan number: Moodle user id}``.
    """
    settings = _settings(stored["manifest"])
    regenerated, manifest = simulate_plan(
        structure["quizzes"],
        lessons=structure.get("lessons") or {},
        overviews=structure.get("overviews") or {},
        items=structure.get("items") or {},
        **settings,
    )
    if _strip(regenerated) != _strip(stored["learners"]):
        new, old = _answer_keys(regenerated), _answer_keys(stored["learners"])
        first = next((i for i, (a, b) in enumerate(zip(new, old)) if a != b), min(len(new), len(old)))
        raise PlanMismatch(
            f"Regenerating the plan (seed {settings['seed']}) does not reproduce the stored one "
            f"({len(new)} vs {len(old)} answers; first difference at answer {first}). The "
            "simulator or the course changed since; the probabilities cannot be recovered."
        )
    annotated = json.loads(json.dumps(stored))
    missing = []
    for learner, again in zip(annotated["learners"], regenerated):
        if learner["attempts"]:
            userid = users.get(learner["number"], users.get(str(learner["number"])))
            if userid is None:
                missing.append(learner["number"])
            learner["moodle_userid"] = userid
        for attempt, attempt_again in zip(learner["attempts"], again["attempts"]):
            for answer, answer_again in zip(attempt["answers"], attempt_again["answers"]):
                answer["p_correct"] = answer_again["p_correct"]
    if missing:
        raise PlanMismatch(
            f"{len(missing)} planned learners with answers have no SIM- account in Moodle "
            f"(e.g. number {missing[0]}); was the plan injected into this site?"
        )
    annotated["manifest"]["annotated"] = {
        "p_correct": "regenerated from the seed and checked identical to the stored plan",
        "moodle_userid": "account with ID number SIM-<number>",
        "oracle_auc_all_answers": manifest["ceiling_auc"],
    }
    return annotated


def is_annotated(plan):
    learners = [learner for learner in plan.get("learners", []) if learner.get("attempts")]
    return bool(learners) and all(
        "moodle_userid" in learner
        and all("p_correct" in a for at in learner["attempts"] for a in at["answers"])
        for learner in learners
    )
