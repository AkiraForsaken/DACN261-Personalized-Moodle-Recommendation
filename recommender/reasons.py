"""Turn a candidate into the sentence a learner reads.

Texts use counts the learner can check ("2 of 5 questions"), never a model
probability. A recommendation that rests on a prediction says so. The numbers
behind each sentence stay in the row's ``evidence`` for the teacher view.
"""

from . import schemas


def _quoted(name):
    return f"“{name}”"


def _answers(evidence):
    """'2 of 5 questions' from a KC evidence payload, or None."""
    attempts, correct = evidence.get("attempts"), evidence.get("correct")
    if not attempts or correct is None:
        return None
    return f"{correct} of {attempts} question{'s' if attempts != 1 else ''}"


def _percent(value):
    return None if value is None else f"{round(value * 100)} %"


def _prerequisite_text(evidence, course, kc, quiz_name, answers, again):
    later = _quoted(course.kc_names.get(evidence.get("for_kc_id"), evidence.get("for_kc_id") or ""))
    known = (
        f"you answered {answers} on it correctly"
        if answers
        else "you have not answered questions on it yet"
    )
    return (
        f"You did not pass {quiz_name} {again}. {kc}, from an earlier unit, comes before "
        f"{later}; {known}. Revisit this lesson."
    )


def _contingent_text(code, evidence, course, kc, quiz_name, answers):
    """Texts of a failure branch: true at the moment Moodle shows them (after the fail)."""
    again = "again" if evidence.get("failed_again") else ""
    did_not_pass = f"You did not pass {quiz_name}{' ' + again if again else ''}."
    if code == schemas.REVISE_BEFORE_RETRY:
        basis = f" You have answered {answers} on {kc} correctly." if answers else ""
        return f"{did_not_pass}{basis} This lesson covers {kc}, one of its topics: read it again before you retry."
    if code == schemas.RETRY_QUIZ:
        return f"Once you have reread the lessons, try {quiz_name} again."
    if code == schemas.REVISIT_PREREQUISITE:
        return _prerequisite_text(evidence, course, kc, quiz_name, answers, "again")
    raise ValueError(f"No contingency text for reason code {code!r}")


def reason_text(candidate, course):
    """The learner-facing sentence for one candidate."""
    code, evidence = candidate.reason_code, candidate.evidence
    activity = course.by_module.get(candidate.module_id)
    kc = _quoted(course.kc_names.get(candidate.kc_id, candidate.kc_id or ""))
    quiz = course.by_module.get(
        evidence.get("quiz_module_id") or evidence.get("upcoming_quiz_module_id")
    )
    quiz_name = _quoted(quiz.name) if quiz and quiz.name else "the quiz"
    answers = _answers(evidence)

    if evidence.get("contingent"):
        return _contingent_text(code, evidence, course, kc, quiz_name, answers)
    if code == schemas.REVISIT_PREREQUISITE:
        return _prerequisite_text(evidence, course, kc, quiz_name, answers, again="after rereading its lessons")
    if code == schemas.REVISE_BEFORE_RETRY:
        basis = (
            f"You answered {answers} on {kc} correctly"
            if answers
            else f"{kc} was your weakest point"
        )
        return f"You have not passed {quiz_name} yet. {basis}. Read this lesson again before you retry."
    if code == schemas.RETRY_QUIZ:
        best = _percent(evidence.get("quiz_best_score"))
        needed = _percent(evidence.get("quiz_pass_fraction"))
        score = (
            f" Your best score is {best}; you need {needed} to pass."
            if best and needed
            else ""
        )
        if evidence.get("revised_kcs"):
            return f"You have reread the lessons for {quiz_name}. Try the quiz again.{score}"
        return f"You have not passed {quiz_name} yet. Try it again.{score}"
    if code == schemas.REVIEW_WEAK_KC:
        return f"You answered {answers} on {kc} correctly. Read this lesson again."
    if code == schemas.PRACTICE_WEAK_KC:
        return (
            f"You answered {answers} on {kc} correctly and have read its lesson since. "
            "Practise it again in this quiz."
        )
    if code == schemas.PREPARE_PREDICTED_DIFFICULTY:
        return (
            f"Estimate, not based on your answers: {kc} may be difficult for you. "
            f"Read this lesson before you start {quiz_name}."
        )
    if code == schemas.START_COURSE:
        return "Start here: this is the first activity of the course."
    if code == schemas.CONTINUE_COURSE:
        topic = activity.topic if activity else ""
        return f"Next in {topic}." if topic else "Next in the course."
    if code == schemas.BASELINE_COURSE_ORDER:
        return "Next open activity in course order."
    if code == schemas.BASELINE_POPULARITY:
        return "Completed by many learners of this course."
    if code == schemas.BASELINE_RANDOM:
        return "A randomly chosen open activity."
    raise ValueError(f"No reason text for reason code {code!r}")
