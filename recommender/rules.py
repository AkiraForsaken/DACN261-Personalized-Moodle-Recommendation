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
    practice_weak_kc     the same, lesson already read: practise in the KC's
                         practice quiz (else its check quiz)

    Retry cap: quizzes allow unlimited attempts. After
    config.MAX_FAILED_CHECK_ATTEMPTS failed attempts of one check quiz in a
    row, the quiz is "stuck": no revise/retry for it, its earlier-unit
    prerequisite lessons are offered once (revisit_prerequisite with
    ``retry_cap_reached``), its weak KCs go to low-stakes practice, and the
    first forward item says the learner may move on (``stuck_quiz_module_id``,
    shown to the teacher). Passing another check quiz afterwards releases it
    for one more retry.

Forward (the learner's way through the course)

    prepare_predicted_difficulty  the model predicts trouble with a KC of the
                                  quiz the learner is about to take, and its
                                  lesson has not been read
    continue_course               next activity in course order from the
                                  learner's current unit
    start_course                  the learner has done nothing yet
    challenge_strong_kc           a KC with enough answers at high accuracy
                                  whose check is passed: its challenge quiz
                                  (harder pool questions), second in the
                                  forward items, at most config.MAX_CHALLENGE

    Quiz roles (layout kc): only check quizzes are "the upcoming quiz"; a
    KC's practice quiz drops out of course order once its check is passed; a
    unit's check-in once any check of that unit was attempted; challenge
    quizzes appear only through challenge_strong_kc. A course without roles
    has only check quizzes.

Contingency (precomputed, shown by Moodle only after a failed attempt)

    For every check quiz not yet passed, the branch "if this quiz is failed": its KC
    lessons, weakest first, then the quiz again. For a quiz that is already
    failed, the branch for failing it again: the weakest KC's lesson, the
    earlier-unit prerequisites, the other weak lessons, the retry. Moodle
    activates a branch the moment the quiz is failed, so the list reacts
    before the engine has refreshed the learner (or while it is down). A
    failure that would reach the retry cap has no retry row; a stuck quiz has
    no branch.

Predictions are deliberately confined to the upcoming quiz. A deep model can
flag most of the course for one learner; sending a Unit 1 learner to Unit 7
lessons on that basis would be neither explainable nor useful.
"""

from dataclasses import dataclass, field

from . import config, schemas
from .course import QUIZ_CHALLENGE, QUIZ_CHECKIN

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


def failed_streak(learner, quiz_module):
    """Failed attempts of the quiz in a row, counted from the latest one.

    Needs the attempt history; without it the count is 0 (no cap applies).
    """
    state = learner.activity(quiz_module)
    streak = 0
    for _, passed in reversed(state.attempts if state else ()):
        if passed:
            break
        streak += 1
    return streak


def _released(learner, course, quiz_module):
    """Has the learner passed another check quiz since this quiz's last failure?"""
    state = learner.activity(quiz_module)
    if state is None or not state.attempts:
        return False
    last_failure = state.attempts[-1][0]
    return any(
        passed and finished > last_failure
        for module_id, other in learner.activities.items()
        if module_id != quiz_module and course.is_check_quiz(module_id)
        for finished, passed in other.attempts
    )


def stuck_quizzes(learner, course):
    """``{quiz_module: failed_in_a_row}`` of check quizzes at the retry cap."""
    stuck = {}
    for module_id, state in learner.activities.items():
        if state.status != STATUS_FAILED or not course.is_check_quiz(module_id):
            continue
        streak = failed_streak(learner, module_id)
        if streak >= config.MAX_FAILED_CHECK_ATTEMPTS and not _released(learner, course, module_id):
            stuck[module_id] = streak
    return stuck


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
            if state.status == STATUS_FAILED
            and module_id in course.by_module
            and course.is_check_quiz(module_id)
        ),
        key=lambda module_id: _sequence(course, module_id),
    )
    stuck = stuck_quizzes(learner, course)
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
        if quiz_module in stuck:
            # Retry cap reached: no revise/retry. Earlier-unit lessons not yet
            # read since the last attempt, once; the quiz's own weak KCs stay
            # uncovered, so they go to low-stakes practice below.
            for index, pick in enumerate(
                _prerequisites(learner, course, quiz_module, weak, quiz.last_attempt)
            ):
                covered.add(pick[0])
                candidates.append(
                    _prerequisite_candidate(
                        pick, quiz_module,
                        priority=(0, quiz.best_score or 0.0, _sequence(course, quiz_module), index),
                    )
                )
                candidates[-1].evidence.update(
                    quiz_evidence, retry_cap_reached=True, quiz_failed_in_a_row=stuck[quiz_module]
                )
            continue
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
        # Low-stakes practice where the course has it, else the check quiz.
        quiz_module = course.practice_of_kc.get(kc_id) or course.quiz_of_kc.get(kc_id)
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
    them); a lesson or practice quiz whose KC's check quiz is already passed,
    because its purpose is met and it would not be "next"; a unit's check-in
    once the learner has attempted a check of that unit (it diagnoses before
    teaching); and challenge quizzes (only ``challenge_strong_kc`` offers them).
    """
    attempted_sections = {
        course.by_module[module_id].section
        for module_id, state in learner.activities.items()
        if state.attempt_count and course.is_check_quiz(module_id)
    }
    practice_kc = {module_id: kc for kc, module_id in course.practice_of_kc.items()}
    for activity in course.from_section_of(learner.last_touched_module):
        state = learner.activity(activity.module_id)
        if state is not None and (state.done or state.status == STATUS_FAILED):
            continue
        role = course.quiz_role(activity.module_id)
        if role == QUIZ_CHALLENGE:
            continue
        if role == QUIZ_CHECKIN and activity.section in attempted_sections:
            continue
        kc_id = course.kc_of_lesson.get(activity.module_id) or practice_kc.get(activity.module_id)
        if kc_id is not None:
            quiz_module = course.quiz_of_kc.get(kc_id)
            if quiz_module is not None and learner.is_done(quiz_module):
                continue
        yield activity


def challenge_candidates(learner, course, max_challenge=config.MAX_CHALLENGE):
    """Harder practice for KCs the learner has shown to be strong in.

    A KC qualifies with at least ``CHALLENGE_MIN_ANSWERS`` answers at
    ``CHALLENGE_MIN_ACCURACY`` or more, its check quiz passed, and a challenge
    quiz the learner has not attempted. The KC whose check was passed most
    recently comes first (it is fresh in mind).
    """
    picks = []
    for kc_id, challenge in course.challenge_of_kc.items():
        state = learner.kcs.get(kc_id)
        check = course.quiz_of_kc.get(kc_id)
        if (
            state is None
            or state.attempts < config.CHALLENGE_MIN_ANSWERS
            or state.observed_accuracy is None
            or state.observed_accuracy < config.CHALLENGE_MIN_ACCURACY
            or check is None
            or learner.activity(check) is None
            or learner.activity(check).status != "passed"
        ):
            continue
        done = learner.activity(challenge)
        if done is not None and (done.attempt_count or done.done):
            continue
        picks.append((learner.activity(check).last_attempt or 0, kc_id, challenge, state))
    picks.sort(key=lambda pick: (-pick[0], pick[1]))
    return [
        Candidate(
            module_id=challenge,
            reason_code=schemas.CHALLENGE_STRONG_KC,
            kc_id=kc_id,
            evidence={**_kc_evidence(kc_id, state), "check_quiz_module_id": course.quiz_of_kc[kc_id]},
        )
        for _, kc_id, challenge, state in picks[:max_challenge]
    ]


def forward_candidates(learner, course, max_prepare):
    """Prepare / continue / start candidates, in the order the learner meets them."""
    open_activities = list(_open_activities(learner, course))
    upcoming_quiz = next(
        (a.module_id for a in open_activities if course.is_check_quiz(a.module_id)), None
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
    challenges = challenge_candidates(learner, course)
    if challenges:
        position = 1 if candidates else 0
        candidates[position:position] = challenges
    return candidates


def mark_stuck(chosen, learner, course):
    """Say on the first "continue" item of a ranked list why the list moves on.

    Applied after ranking, so the note lands on an item that is really in the
    list (an activity that is also a remediation item keeps that reason).
    Returns ``chosen``.
    """
    stuck = stuck_quizzes(learner, course)
    if not stuck:
        return chosen
    quiz_module = min(stuck, key=lambda module_id: _sequence(course, module_id))
    first = next(
        (
            c for c in chosen
            if c.reason_code == schemas.CONTINUE_COURSE and c.trigger_module_id is None
        ),
        None,
    )
    if first is not None:
        first.evidence = {
            **first.evidence,
            "stuck_quiz_module_id": quiz_module,
            "stuck_failed_attempts": stuck[quiz_module],
        }
    return chosen


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
    stuck = stuck_quizzes(learner, course)
    for quiz_module in course.check_quizzes():
        if learner.is_done(quiz_module) or quiz_module in stuck:
            continue
        state = learner.activity(quiz_module)
        failed_now = state is not None and state.status == STATUS_FAILED
        # Failing once more reaches the cap: the branch then has no retry row.
        at_cap_next = (
            failed_now
            and failed_streak(learner, quiz_module) + 1 >= config.MAX_FAILED_CHECK_ATTEMPTS
            and not _released(learner, course, quiz_module)
        )
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
        if not at_cap_next:
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
