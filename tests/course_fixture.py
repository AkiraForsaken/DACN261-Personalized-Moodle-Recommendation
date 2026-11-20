"""A small Moodle course export in the collector's exact file format.

Two units, each: overview page, two KC lessons, a practice quiz with four
questions per KC (pass mark 70 %). Learners follow scripted histories, so the
whole pipeline (knowledge engine -> learner model -> recommender -> bundle)
can run on it, for one learner or for all of them.

    learner 7   real, enrolled, nothing done (cold start)
    learner 8   real, overview + lesson KC1, quiz 1 failed (weak on KC1)
    learner 9   real, unit 1 passed (KC2 below the weak-area threshold), unit 2 started
    learner 10  simulated, everything passed
    learner 11  real, quiz 1 failed, reread the lesson, retried and passed
"""

import json
from pathlib import Path

import pandas as pd

from collector.collect_question_answers import ANSWER_EVENT_COLUMNS, ROSTER_COLUMNS
from collector.sessions import assign_sessions, summarize_sessions

# Header rows of the collector's per-learner files (written even when empty).
EVENT_COLUMNS = [
    "log_id", "learner_id", "course_id", "course_module_id", "cmid_source", "event_name",
    "event_category", "is_learning_event", "action", "target", "object_table", "object_id",
    "context_id", "context_level", "context_instance_id", "timestamp", "timestamp_unix",
    "seconds_since_prev_event", "session_id", "session_event_seq", "is_session_start",
]
SESSION_COLUMNS = [
    "session_id", "learner_id", "course_id", "start_time", "end_time", "start_time_unix",
    "end_time_unix", "duration_seconds", "event_count", "learning_event_count",
    "question_interactions", "distinct_modules", "topics",
]
ATTEMPT_COLUMNS = [
    "learner_id", "quiz_id", "quiz_name", "topic", "sequence_position", "attempt", "state",
    "score", "max_score", "normalized_score", "mastery_threshold_reached", "start_time",
    "finish_time", "duration_seconds", "start_time_unix", "finish_time_unix",
]
COMPLETION_COLUMNS = [
    "learner_id", "course_module_id", "activity_type", "activity_name", "topic", "sequence",
    "completion_state", "completion_state_label", "viewed", "overridden_by", "time_modified",
    "time_modified_unix",
]

COURSE_ID = 42
T0 = 1_790_000_000
MAPPING_VERSION = "qcat-fixture"
PASS = 0.7

UNITS = {
    1: {"topic": "Unit 1: Routines", "overview": 101, "lessons": {"KC1": 102, "KC2": 103}, "quiz": 104},
    2: {"topic": "Unit 2: Work", "overview": 201, "lessons": {"KC3": 202, "KC4": 203}, "quiz": 204},
}
KCS = {
    "KC1": ("Present simple -s", "grammar", 1),
    "KC2": ("Routine verbs", "vocabulary", 1),
    "KC3": ("Past simple", "grammar", 2),
    "KC4": ("Office words", "vocabulary", 2),
}
QUESTIONS = {kc: [1000 * unit + 10 * index + q for q in range(4)] for index, (kc, (_, _, unit)) in enumerate(KCS.items(), 1)}
QUIZ_INSTANCE = {104: 501, 204: 502}

# learner -> (is_simulated, [actions]); an action is
#   ("view", cmid, minutes)  or  ("quiz", cmid, minutes, {kc: correct_of_4})
HISTORIES = {
    7: (0, []),
    8: (0, [("view", 101, 0), ("view", 102, 5), ("quiz", 104, 20, {"KC1": 1, "KC2": 3})]),
    9: (0, [
        ("view", 101, 0), ("view", 102, 4), ("view", 103, 9),
        ("quiz", 104, 20, {"KC1": 4, "KC2": 2}),
        ("view", 201, 2000),
    ]),
    10: (1, [
        ("view", 101, 0), ("view", 102, 3), ("view", 103, 6), ("quiz", 104, 15, {"KC1": 4, "KC2": 4}),
        ("view", 201, 1500), ("view", 202, 1503), ("view", 203, 1506), ("quiz", 204, 1520, {"KC3": 3, "KC4": 4}),
    ]),
    11: (0, [
        ("view", 101, 0), ("quiz", 104, 10, {"KC1": 1, "KC2": 4}),
        ("view", 102, 3000), ("quiz", 104, 3020, {"KC1": 3, "KC2": 4}),
    ]),
}


def _activity_catalog():
    rows = [dict(course_module_id=100, section_number=0, activity_type="forum", activity_id=1,
                 activity_name="Announcements", topic="General", completion_tracking=0, idnumber="")]
    for unit, spec in UNITS.items():
        rows.append(dict(course_module_id=spec["overview"], section_number=unit, activity_type="page",
                         activity_id=spec["overview"], activity_name=f"Unit {unit} overview",
                         topic=spec["topic"], completion_tracking=2, idnumber=""))
        for kc, cmid in spec["lessons"].items():
            rows.append(dict(course_module_id=cmid, section_number=unit, activity_type="page",
                             activity_id=cmid, activity_name=f"Lesson: {KCS[kc][0]}",
                             topic=spec["topic"], completion_tracking=2, idnumber=kc))
        rows.append(dict(course_module_id=spec["quiz"], section_number=unit, activity_type="quiz",
                         activity_id=QUIZ_INSTANCE[spec["quiz"]], activity_name=f"Unit {unit} practice",
                         topic=spec["topic"], completion_tracking=2, idnumber=""))
    frame = pd.DataFrame(rows)
    frame["section_id"] = frame["section_number"] + 900
    frame["sequence"] = range(1, len(frame) + 1)
    frame["visible"] = 1
    frame["pass_fraction"] = frame["activity_type"].map({"quiz": PASS})
    return frame[["course_module_id", "section_id", "section_number", "activity_type", "activity_id",
                  "activity_name", "topic", "sequence", "visible", "completion_tracking", "idnumber",
                  "pass_fraction"]]


def _course_maps():
    catalog = pd.DataFrame(
        [dict(kc_id=kc, kc_name=name, kc_dimension=dim, learning_objective=f"Use {name}.")
         for kc, (name, dim, _) in KCS.items()]
    )
    question_map = pd.DataFrame(
        [dict(question_bank_entry_id=qid, kc_id=kc, kc_dimension=KCS[kc][1],
              mapping_version=MAPPING_VERSION, mapping_source="question_category_idnumber")
         for kc, qids in QUESTIONS.items() for qid in qids]
    )
    topic_map = pd.DataFrame(
        [dict(kc_id=kc, topic=UNITS[unit]["topic"], mapping_version=MAPPING_VERSION)
         for kc, (_, _, unit) in KCS.items()]
    )
    links = []
    for unit, spec in UNITS.items():
        for kc, cmid in spec["lessons"].items():
            links.append(dict(kc_id=kc, course_module_id=cmid, activity_type="page",
                              activity_name=f"Lesson: {KCS[kc][0]}", topic=spec["topic"], role="learn",
                              question_count=None, mapping_source="course_module_idnumber",
                              mapping_version=MAPPING_VERSION))
            links.append(dict(kc_id=kc, course_module_id=spec["quiz"], activity_type="quiz",
                              activity_name=f"Unit {unit} practice", topic=spec["topic"], role="practice",
                              question_count=4, mapping_source="quiz_question_usage",
                              mapping_version=MAPPING_VERSION))
    return catalog, question_map, topic_map, pd.DataFrame(links)


def _learner_records(learner, history, catalog):
    by_cmid = catalog.set_index("course_module_id")
    events, attempts, answers, completion = [], [], [], {}
    attempt_numbers = {}
    for action in history:
        kind, cmid, minutes = action[:3]
        when = T0 + 60 * minutes
        topic = by_cmid.loc[cmid, "topic"]
        name = by_cmid.loc[cmid, "activity_name"]
        if kind == "view":
            events.append(dict(cmid=cmid, when=when, name=r"\mod_page\event\course_module_viewed",
                               category="content_view", topic=topic))
            completion.setdefault(cmid, (1, when))
            continue
        scores = action[3]
        attempt_numbers[cmid] = attempt_numbers.get(cmid, 0) + 1
        number = attempt_numbers[cmid]
        events.append(dict(cmid=cmid, when=when, name=r"\mod_quiz\event\course_module_viewed",
                           category="content_view", topic=topic))
        events.append(dict(cmid=cmid, when=when + 30, name=r"\mod_quiz\event\attempt_started",
                           category="quiz_attempt", topic=topic))
        finish = when + 300
        events.append(dict(cmid=cmid, when=finish, name=r"\mod_quiz\event\attempt_submitted",
                           category="quiz_attempt", topic=topic))
        attempt_id = learner * 1000 + cmid * 10 + number
        right = total = 0
        for kc, correct_of_4 in scores.items():
            for index, qid in enumerate(QUESTIONS[kc]):
                correct = int(index < correct_of_4)
                right += correct
                total += 1
                answers.append(dict(
                    learner_id=learner, item_id=f"moodle-qbe:{qid}", question_bank_entry_id=qid,
                    kc_id=kc, kc_dimension=KCS[kc][1], event_type="answer",
                    timestamp=when + 40 + 20 * total, response_time_s=20, correct=correct,
                    raw_fraction=float(correct), max_mark=1.0, quiz_attempt_id=attempt_id,
                    question_attempt_id=attempt_id * 100 + total, quiz_id=QUIZ_INSTANCE[cmid],
                    attempt_number=number, mapping_version=MAPPING_VERSION,
                    mapping_source="question_category_idnumber",
                ))
        score = right / total
        attempts.append(dict(
            learner_id=learner, quiz_id=QUIZ_INSTANCE[cmid], quiz_name=name, topic=topic,
            sequence_position=int(by_cmid.loc[cmid, "sequence"]), attempt=number, state="finished",
            score=float(right), max_score=float(total), normalized_score=score,
            mastery_threshold_reached=int(score >= 0.8), start_time="", finish_time="",
            duration_seconds=270, start_time_unix=when + 30, finish_time_unix=finish,
        ))
        previous_best = completion[cmid][2] if cmid in completion else 0.0
        best = max(score, previous_best)
        # Moodle's quiz completion: complete_pass (2) once the best grade passes, else complete_fail (3).
        completion[cmid] = (2 if best >= PASS else 3, finish, best)
    return events, attempts, answers, completion


def write_course_export(out_dir, learners=None):
    """Write the export; ``learners`` limits per-learner rows like ``collector --learners``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    catalog = _activity_catalog()
    kc_catalog, question_map, topic_map, links = _course_maps()
    catalog.to_csv(out / "activity_catalog.csv", index=False)
    kc_catalog.to_csv(out / "kc_catalog.csv", index=False)
    question_map.to_csv(out / "moodle_question_kc_map.csv", index=False)
    topic_map.to_csv(out / "kc_topic_map.csv", index=False)
    links.to_csv(out / "kc_activity_map.csv", index=False)

    chosen = sorted(HISTORIES) if learners is None else sorted(int(x) for x in learners)
    roster, event_rows, attempt_rows, answer_rows, completion_rows = [], [], [], [], []
    log_id = 0
    for learner in chosen:
        simulated, history = HISTORIES[learner]
        roster.append(dict(learner_id=learner, course_id=COURSE_ID, enrol_start_unix=T0 - 86_400,
                           enrol_end_unix=0, is_simulated=simulated))
        events, attempts, answers, completion = _learner_records(learner, history, catalog)
        for event in events:
            log_id += 1
            event_rows.append(dict(
                log_id=log_id, learner_id=learner, course_id=COURSE_ID, course_module_id=event["cmid"],
                cmid_source="module_context", event_name=event["name"], event_category=event["category"],
                is_learning_event=1, action="", target="", object_table="", object_id="",
                context_id="", context_level=70, context_instance_id=event["cmid"], timestamp="",
                timestamp_unix=event["when"], _topic=event["topic"],
            ))
        attempt_rows += attempts
        for index, answer in enumerate(answers, start=1):
            answer_rows.append({**answer, "event_index": index})
        for cmid, (state, when, *_) in completion.items():
            completion_rows.append(dict(
                learner_id=learner, course_module_id=cmid, activity_type="", activity_name="",
                topic="", sequence="", completion_state=state, completion_state_label="",
                viewed=1, overridden_by="", time_modified="", time_modified_unix=when,
            ))

    sessions = summarize_sessions(assign_sessions(event_rows))
    for row in event_rows:
        row.pop("_topic", None)
    pd.DataFrame(roster, columns=ROSTER_COLUMNS).to_csv(out / "learner_roster.csv", index=False)
    pd.DataFrame(event_rows, columns=EVENT_COLUMNS).to_csv(out / "learning_events.csv", index=False)
    pd.DataFrame(sessions, columns=SESSION_COLUMNS).to_csv(out / "learning_sessions.csv", index=False)
    pd.DataFrame(attempt_rows, columns=ATTEMPT_COLUMNS).to_csv(out / "quiz_attempts.csv", index=False)
    pd.DataFrame(answer_rows, columns=ANSWER_EVENT_COLUMNS).to_csv(
        out / "question_answer_events.csv", index=False
    )
    pd.DataFrame(completion_rows, columns=COMPLETION_COLUMNS).to_csv(
        out / "course_modules_completion.csv", index=False
    )
    if any(HISTORIES[learner][0] for learner in chosen):
        (out / "simulation_manifest.json").write_text(json.dumps({"simulated": True}))
    (out / "collection_manifest.json").write_text(json.dumps({
        "course_id": COURSE_ID, "collected_at": T0 + 86_400 * 10,
        "learners": None if learners is None else chosen,
    }))
    return out
