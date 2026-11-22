"""Candidate generation: one small rule per reason code.

Each rule looks at one learner's state and the course and proposes
``Candidate`` activities. Nothing here ranks or truncates; ``ranking.py`` turns
the two candidate lists into the final top-N.

Remediation (rests on the learner's own graded answers)

    revise_before_retry  a quiz is currently failed, and the lesson of one of
                         its weak KCs has not been read since the last attempt
    retry_quiz           a quiz is currently failed: queued right after its
                         revise/prerequisite lessons (shown once they are read)
    revisit_prerequisite the quiz was failed again although the learner read
                         a weak KC's lesson between the two attempts: go back
                         to KCs of the same dimension in earlier units that
                         are weak or not assessed (a course-design assumption,
                         config.PREREQUISITE_RELATION)
    review_weak_kc       an evidence-based concern on a KC outside a failed
                         quiz; its lesson has not been read since the last answer
    practice_weak_kc     the same, lesson already read: practise in the quiz

Forward (the learner's way through the course)

    prepare_predicted_difficulty  the model predicts trouble with a KC of the
                                  quiz the learner is about to take, and its
                                  lesson has not been read
    continue_course               next activity in course order from the
                                  learner's current unit
    start_course                  the learner has done nothing yet

Contingency (precomputed, shown by Moodle only after a failed attempt)

    For every quiz not yet passed, the branch "if this quiz is failed": its KC
    lessons, weakest first, then the quiz again. For a quiz that is already
    failed, the branch for failing it again: the weakest KC's lesson, the
    earlier-unit prerequisites, the other weak lessons, the retry. Moodle
    activates a branch the moment the quiz is failed, so the list reacts
    before the engine has refreshed the learner (or while it is down).

Predictions are deliberately confined to the upcoming quiz. A deep model can
flag most of the course for one learner; sending a Unit 1 learner to Unit 7
lessons on that basis would be neither explainable nor useful.
"""

from dataclasses import dataclass, field

from . import config, schemas

EVIDENCE_CONCERN = "evidence_based_concern"
PREDICTION_CONCERN = "prediction_based_concern"
STATUS_FAILED = "failed"
# Priority suffix that places a failed quiz's retry after its own lessons.
_AFTER_LESSONS = 1_000


@dataclass
class Candidate:
    module_id: int
    reason_code: str
    kc_id: str | None = None
    # Smaller sorts first among remediation candidates.
    priority: tuple = ()
    evidence: dict = field(default_factory=dict)
    # Contingency rows only: the quiz whose failure activates this row.
    trigger_module_id: int | None = None


def _round(value):
    return None if value is None else round(float(value), 4)


def _kc_evidence(kc_id, state):
    return {
        "kc_id": kc_id,
        "attempts": state.attempts,
        "correct": state.correct,
        "observed_accuracy": _round(state.observed_accuracy),
        "predicted_correctness": _round(state.predicted_correctness),
    }


def _read_since(learner, lesson_module, since):
    """Was the lesson viewed after ``since`` (Unix seconds)?"""
    lesson = learner.activity(lesson_module)
    if lesson is None or lesson.last_viewed is None:
        return False
    return since is None or lesson.last_viewed > since


def _sequence(course, module_id):
    activity = course.by_module.get(module_id)
    return activity.sequence if activity else 10**9


def _weak_kcs_of_failed_quiz(learner, course, quiz_module, low_accuracy):
    """KCs of the quiz answered below the threshold; else its single weakest KC."""
    answered = [
        (kc_id, learner.kcs[kc_id])
        for kc_id in course.kcs_of_quiz.get(quiz_module, [])
        if kc_id in learner.kcs
        and learner.kcs[kc_id].attempts > 0
        and learner.kcs[kc_id].observed_accuracy is not None
    ]
    weak = [item for item in answered if item[1].observed_accuracy < low_accuracy]
    if not weak and answered:
        weak = [min(answered, key=lambda item: item[1].observed_accuracy)]
    return sorted(weak, key=lambda item: (item[1].observed_accuracy, item[0]))


def _failed_after_revising(learner, course, quiz_module, weak):
    """Latest attempt failed although a weak KC's lesson was read since the one before.

    Needs the attempt history (``learner_quiz_attempts.csv``) and every view
    time; without them it is False and the ordinary revise/retry loop applies.
    """
    attempts = learner.activities[quiz_module].attempts
    if len(attempts) < 2 or attempts[-1][1]:
        return False
    previous, latest = attempts[-2][0], attempts[-1][0]
    for kc_id, _ in weak:
        lesson = learner.activity(course.lesson_of_kc.get(kc_id))
        if lesson is not None and lesson.viewed_between(previous, latest):
            return True
    return False


def _prerequisites(learner, course, quiz_module, weak, since, exclude=()):
    """Up to MAX_PREREQUISITES earlier-unit lessons for the quiz's weak KCs.

    A prerequisite KC qualifies when it is an evidence-based concern or has no
    answers yet. Nearest unit first, weak before not assessed, then course
    order. Lessons read after ``since`` are skipped (None: none skipped).
    """
    picks, seen = [], set(exclude)
    for kc_id, _ in weak:
        for other in course.prerequisite_kcs(kc_id):
            lesson = course.lesson_of_kc.get(other)
            if lesson is None or lesson in seen:
                continue
            state = learner.kcs.get(other)
            is_weak = state is not None and state.concern == EVIDENCE_CONCERN
            untested = state is None or not state.attempts
            if not (is_weak or untested) or (since is not None and _read_since(learner, lesson, since)):
                continue
            seen.add(lesson)
            picks.append((other, lesson, state, is_weak, kc_id))
    picks.sort(
        key=lambda pick: (-course.unit_of_kc(pick[0]), not pick[3], _sequence(course, pick[1]))
    )
    return picks[: config.MAX_PREREQUISITES]


def _prerequisite_candidate(pick, quiz_module, priority=(), trigger=None):
    kc_id, lesson, state, is_weak, for_kc = pick
    evidence = (
        _kc_evidence(kc_id, state)
        if state is not None
        else {"kc_id": kc_id, "attempts": 0, "correct": None,
              "observed_accuracy": None, "predicted_correctness": None}
    )
    return Candidate(
        module_id=lesson,
        reason_code=schemas.REVISIT_PREREQUISITE,
        kc_id=kc_id,
        priority=priority,
        evidence={
            **evidence,
            "quiz_module_id": quiz_module,
            "for_kc_id": for_kc,
            "prerequisite_basis": "weak" if is_weak else "not_assessed",
            "relation": config.PREREQUISITE_RELATION,
            **({"contingent": True} if trigger is not None else {}),
        },
        trigger_module_id=trigger,
    )


def remediation_candidates(learner, course, low_accuracy):
    """Revise / retry / review / practise candidates, unranked."""
    candidates, covered = [], set()

    failed_quizzes = sorted(
        (
            module_id
            for module_id, state in learner.activities.items()
            if state.status == STATUS_FAILED and module_id in course.by_module
        ),
        key=lambda module_id: _sequence(course, module_id),
    )
    for quiz_module in failed_quizzes:
        quiz = learner.activities[quiz_module]
        quiz_evidence = {
            "quiz_module_id": quiz_module,
            "quiz_attempts": quiz.attempt_count,
            "quiz_best_score": _round(quiz.best_score),
            "quiz_pass_fraction": _round(quiz.pass_fraction),
        }
        pending = []
        weak = _weak_kcs_of_failed_quiz(learner, course, quiz_module, low_accuracy)
        for kc_id, state in weak:
            covered.add(kc_id)
            lesson = course.lesson_of_kc.get(kc_id)
            if lesson is None or _read_since(learner, lesson, quiz.last_attempt):
                continue
            pending.append(
                Candidate(
                    module_id=lesson,
                    reason_code=schemas.REVISE_BEFORE_RETRY,
                    kc_id=kc_id,
                    priority=(0, state.observed_accuracy, _sequence(course, lesson)),
                    evidence={**_kc_evidence(kc_id, state), **quiz_evidence},
                )
            )
        retry_priority = (0, quiz.best_score or 0.0, _sequence(course, quiz_module))
        if _failed_after_revising(learner, course, quiz_module, weak):
            # Revising did not help: also go back to earlier units. Placed
            # right after the weakest lesson (or before the retry).
            anchor = min(c.priority for c in pending) if pending else retry_priority[:2] + (
                retry_priority[2] - 0.5,
            )
            for index, pick in enumerate(
                _prerequisites(learner, course, quiz_module, weak, quiz.last_attempt)
            ):
                covered.add(pick[0])
                candidates.append(
                    _prerequisite_candidate(pick, quiz_module, priority=anchor + (1 + index,))
                )
                candidates[-1].evidence.update(quiz_evidence, failed_after_revising=True)
        candidates.extend(pending)
        # The retry is always queued, right after this quiz's own lessons and
        # prerequisites. Before, it appeared only once nothing was left to
        # revise, so after rereading the lesson the failed quiz dropped out of
        # the list until the next refresh (reading a lesson does not trigger one).
        siblings = [
            c.priority
            for c in candidates
            if c.evidence.get("quiz_module_id") == quiz_module and c.priority
        ]
        candidates.append(
            Candidate(
                module_id=quiz_module,
                reason_code=schemas.RETRY_QUIZ,
                priority=max(siblings) + (_AFTER_LESSONS,) if siblings else retry_priority,
                evidence={
                    **quiz_evidence,
                    **(
                        {"lessons_pending": len(pending)}
                        if pending
                        else {"revised_kcs": [kc_id for kc_id, _ in weak]}
                    ),
                },
            )
        )

    practice = {}  # quiz module -> its weakest concerned KC
    for kc_id, state in learner.kcs.items():
        if state.concern != EVIDENCE_CONCERN or kc_id in covered:
            continue
        accuracy = state.observed_accuracy if state.observed_accuracy is not None else 1.0
        lesson = course.lesson_of_kc.get(kc_id)
        quiz_module = course.quiz_of_kc.get(kc_id)
        if lesson is not None and not _read_since(learner, lesson, state.last_attempt):
            candidates.append(
                Candidate(
                    module_id=lesson,
                    reason_code=schemas.REVIEW_WEAK_KC,
                    kc_id=kc_id,
                    priority=(1, accuracy, _sequence(course, lesson)),
                    evidence=_kc_evidence(kc_id, state),
                )
            )
        elif quiz_module is not None:
            best = practice.get(quiz_module)
            if best is None or accuracy < best[0]:
                practice[quiz_module] = (accuracy, kc_id, state)
    for quiz_module, (accuracy, kc_id, state) in practice.items():
        candidates.append(
            Candidate(
                module_id=quiz_module,
                reason_code=schemas.PRACTICE_WEAK_KC,
                kc_id=kc_id,
                priority=(1, accuracy, _sequence(course, quiz_module)),
                evidence={**_kc_evidence(kc_id, state), "quiz_module_id": quiz_module},
            )
        )
    return candidates


def _open_activities(learner, course):
    """Activities still ahead of the learner, from their current unit onwards.

    Skipped: activities already done; failed quizzes (remediation handles
    them); and a lesson whose practice quiz is already passed, because the
    lesson's purpose is met and reading it would not be "next".
    """
    for activity in course.from_section_of(learner.last_touched_module):
        state = learner.activity(activity.module_id)
        if state is not None and (state.done or state.status == STATUS_FAILED):
            continue
        kc_id = course.kc_of_lesson.get(activity.module_id)
        if kc_id is not None:
            quiz_module = course.quiz_of_kc.get(kc_id)
            if quiz_module is not None and learner.is_done(quiz_module):
                continue
        yield activity


def forward_candidates(learner, course, max_prepare):
    """Prepare / continue / start candidates, in the order the learner meets them."""
    open_activities = list(_open_activities(learner, course))
    upcoming_quiz = next(
        (a.module_id for a in open_activities if a.activity_type == "quiz"), None
    )
    # Lessons still open; a lesson already read is not "preparation" any more.
    open_lesson_kcs = {
        course.kc_of_lesson[a.module_id]
        for a in open_activities
        if a.module_id in course.kc_of_lesson
        and not _read_since(learner, a.module_id, None)
    }
    predicted = sorted(
        (
            (learner.kcs[kc_id].predicted_correctness, kc_id)
            for kc_id in course.kcs_of_quiz.get(upcoming_quiz, [])
            if kc_id in open_lesson_kcs
            and kc_id in learner.kcs
            and learner.kcs[kc_id].concern == PREDICTION_CONCERN
            and learner.kcs[kc_id].predicted_correctness is not None
        )
    )
    prepare_kcs = {kc_id for _, kc_id in predicted[:max_prepare]}

    nothing_done = not learner.activities
    candidates = []
    for position, activity in enumerate(open_activities):
        kc_id = course.kc_of_lesson.get(activity.module_id)
        if kc_id in prepare_kcs:
            candidates.append(
                Candidate(
                    module_id=activity.module_id,
                    reason_code=schemas.PREPARE_PREDICTED_DIFFICULTY,
                    kc_id=kc_id,
                    evidence={
                        **_kc_evidence(kc_id, learner.kcs[kc_id]),
                        "upcoming_quiz_module_id": upcoming_quiz,
                        "estimator_phase": learner.phase,
                    },
                )
            )
            continue
        first_ever = nothing_done and position == 0
        candidates.append(
            Candidate(
                module_id=activity.module_id,
                reason_code=schemas.START_COURSE if first_ever else schemas.CONTINUE_COURSE,
                kc_id=kc_id,
                evidence={"position_in_course": activity.sequence},
            )
        )
    return candidates


def _estimated_correctness(state):
    """Observed accuracy where the learner answered, else the model estimate."""
    if state is None:
        return 1.0
    if state.attempts and state.observed_accuracy is not None:
        return state.observed_accuracy
    return state.predicted_correctness if state.predicted_correctness is not None else 1.0


def contingency_candidates(learner, course, low_accuracy, max_lessons=config.MAX_REMEDIATION):
    """The precomputed "if this quiz is failed" branches, in course order.

    Every candidate carries ``trigger_module_id`` (the quiz). Moodle shows a
    branch only after the learner fails that quiz, until the engine refreshes
    the learner with the real attempt.
    """
    branches = []
    quizzes = sorted(
        (q for q in course.kcs_of_quiz if q in course.by_module),
        key=lambda q: _sequence(course, q),
    )
    for quiz_module in quizzes:
        if learner.is_done(quiz_module):
            continue
        state = learner.activity(quiz_module)
        failed_now = state is not None and state.status == STATUS_FAILED
        base = {"quiz_module_id": quiz_module, "contingent": True}
        lessons = []
        if failed_now:
            # Failing it again: the weakest KC, the earlier units, the rest.
            weak = _weak_kcs_of_failed_quiz(learner, course, quiz_module, low_accuracy)
            base["failed_again"] = True
            revise = [kc for kc, _ in weak if kc in course.lesson_of_kc]
            prerequisites = [
                _prerequisite_candidate(pick, quiz_module, trigger=quiz_module)
                for pick in _prerequisites(
                    learner, course, quiz_module, weak, since=None,
                    exclude=[course.lesson_of_kc[kc] for kc in revise],
                )
            ]
            for candidate in prerequisites:
                candidate.evidence.update(base)
        else:
            ranked = sorted(
                (kc for kc in course.kcs_of_quiz[quiz_module] if kc in course.lesson_of_kc),
                key=lambda kc: (
                    _estimated_correctness(learner.kcs.get(kc)),
                    _sequence(course, course.lesson_of_kc[kc]),
                ),
            )
            revise, prerequisites = ranked[:max_lessons], []
        for kc_id in revise:
            kc_state = learner.kcs.get(kc_id)
            lessons.append(
                Candidate(
                    module_id=course.lesson_of_kc[kc_id],
                    reason_code=schemas.REVISE_BEFORE_RETRY,
                    kc_id=kc_id,
                    evidence={
                        **(_kc_evidence(kc_id, kc_state) if kc_state else {"kc_id": kc_id}),
                        **base,
                    },
                    trigger_module_id=quiz_module,
                )
            )
        branch = lessons[:1] + prerequisites + lessons[1:]
        branch.append(
            Candidate(
                module_id=quiz_module,
                reason_code=schemas.RETRY_QUIZ,
                evidence={
                    **base,
                    "quiz_pass_fraction": _round(state.pass_fraction) if state else None,
                },
                trigger_module_id=quiz_module,
            )
        )
        branches.extend(branch)
    return branches
