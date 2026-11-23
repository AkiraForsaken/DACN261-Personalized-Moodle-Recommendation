"""Recommender: rules, ranking, baselines, the output contract and generation."""

import json

import pandas as pd
import pytest

from recommender import baselines, config, schemas
from recommender.course import build_course_graph
from recommender.generate import generate, recommend, recommend_all
from recommender.inputs import (
    ActivityState,
    KCState,
    LearnerState,
    RecommenderInputs,
    load_inputs,
    sha256_file,
)
from recommender.ranking import order_queue, select, window
from recommender.rules import Candidate, forward_candidates, remediation_candidates

# Course: Unit 1 = overview, lesson A, lesson B, quiz 1 (KC-A, KC-B)
#         Unit 2 = lesson C, quiz 2 (KC-C);  Unit 3 = lesson D, quiz 3 (KC-D)
OVERVIEW, LESSON_A, LESSON_B, QUIZ_1, LESSON_C, QUIZ_2, LESSON_D, QUIZ_3 = range(101, 109)
FORUM = 100


def _catalog():
    rows = [
        (FORUM, 0, "forum", "Announcements", None, 0),
        (OVERVIEW, 1, "page", "Unit 1 overview", "Unit 1", 2),
        (LESSON_A, 1, "page", "Lesson: A", "Unit 1", 2),
        (LESSON_B, 1, "page", "Lesson: B", "Unit 1", 2),
        (QUIZ_1, 1, "quiz", "Unit 1 practice", "Unit 1", 2),
        (LESSON_C, 2, "page", "Lesson: C", "Unit 2", 2),
        (QUIZ_2, 2, "quiz", "Unit 2 practice", "Unit 2", 2),
        (LESSON_D, 3, "page", "Lesson: D", "Unit 3", 2),
        (QUIZ_3, 3, "quiz", "Unit 3 practice", "Unit 3", 2),
    ]
    frame = pd.DataFrame(
        rows,
        columns=[
            "course_module_id",
            "section_number",
            "activity_type",
            "activity_name",
            "topic",
            "completion_tracking",
        ],
    )
    frame["sequence"] = range(1, len(frame) + 1)
    frame["visible"] = 1
    frame["activity_id"] = frame["course_module_id"] - 100
    frame["pass_fraction"] = frame["activity_type"].eq("quiz").map({True: 0.7, False: None})
    return frame


def _kc_map():
    links = [
        ("KC-A", LESSON_A, "learn", None),
        ("KC-B", LESSON_B, "learn", None),
        ("KC-C", LESSON_C, "learn", None),
        ("KC-D", LESSON_D, "learn", None),
        ("KC-A", QUIZ_1, "practice", 4),
        ("KC-B", QUIZ_1, "practice", 4),
        ("KC-C", QUIZ_2, "practice", 4),
        ("KC-D", QUIZ_3, "practice", 4),
    ]
    return pd.DataFrame(links, columns=["kc_id", "course_module_id", "role", "question_count"])


KC_NAMES = {"KC-A": "Topic A", "KC-B": "Topic B", "KC-C": "Topic C", "KC-D": "Topic D"}


def _course():
    return build_course_graph(_catalog(), _kc_map(), KC_NAMES)


def _inputs(*learners):
    return RecommenderInputs(
        course=_course(),
        learners={learner.learner_id: learner for learner in learners},
        low_accuracy=0.6,
        as_of=None,
        mapping_version="map-1",
        input_files={},
    )


def _learner(learner_id="7", phase=1, activities=None, kcs=None):
    return LearnerState(
        learner_id=learner_id,
        population="moodle_collector",
        phase=phase,
        activities=activities or {},
        kcs=kcs or {},
    )


def _codes(candidates):
    return [(c.module_id, c.reason_code) for c in candidates]


def _recommend(learner, top_n=5):
    return _codes(recommend(learner, _inputs(learner), top_n=top_n))


# ------------------------------------------------------------------ the course
def test_course_graph_keeps_only_recommendable_activities_in_course_order():
    course = _course()

    assert [a.module_id for a in course.activities] == list(range(101, 109))  # no forum
    assert course.lesson_of_kc["KC-A"] == LESSON_A
    assert course.quiz_of_kc["KC-B"] == QUIZ_1
    assert course.kcs_of_quiz[QUIZ_1] == ["KC-A", "KC-B"]
    assert course.kc_of_lesson[LESSON_C] == "KC-C"
    # From a Unit 2 activity: Unit 2 first, then on, wrapping to Unit 1.
    order = [a.module_id for a in course.from_section_of(QUIZ_2)]
    assert order == [LESSON_C, QUIZ_2, LESSON_D, QUIZ_3, OVERVIEW, LESSON_A, LESSON_B, QUIZ_1]


# ------------------------------------------------------------------ the rules
def test_a_new_learner_is_sent_to_the_start_of_the_course():
    assert _recommend(_learner()) == [
        (OVERVIEW, schemas.START_COURSE),
        (LESSON_A, schemas.CONTINUE_COURSE),
        (LESSON_B, schemas.CONTINUE_COURSE),
        (QUIZ_1, schemas.CONTINUE_COURSE),
        (LESSON_C, schemas.CONTINUE_COURSE),
    ]


def _failed_quiz_1(lesson_a_viewed_at):
    """Quiz 1 failed at t=200; weak on KC-A (1 of 4), fine on KC-B (3 of 4)."""
    return _learner(
        activities={
            OVERVIEW: ActivityState("completed", last_viewed=10),
            LESSON_A: ActivityState("completed", last_viewed=lesson_a_viewed_at),
            LESSON_B: ActivityState("completed", last_viewed=60),
            QUIZ_1: ActivityState(
                "failed", last_viewed=100, last_attempt=200, attempt_count=1,
                best_score=0.5, pass_fraction=0.7,
            ),
        },
        kcs={
            "KC-A": KCState(4, 0.25, 0.4, 200, "evidence_based_concern"),
            "KC-B": KCState(4, 0.75, 0.7, 200, "no_current_concern"),
        },
    )


def test_failed_quiz_first_sends_the_learner_back_to_the_weak_lesson():
    learner = _failed_quiz_1(lesson_a_viewed_at=50)  # read before the attempt only

    chosen = recommend(learner, _inputs(learner))

    # The lesson first, the retry right after it (so the failed quiz stays in
    # the list once the lesson is reread, without waiting for a refresh); the
    # KC the learner answered well is not flagged.
    assert _codes(chosen)[:2] == [
        (LESSON_A, schemas.REVISE_BEFORE_RETRY),
        (QUIZ_1, schemas.RETRY_QUIZ),
    ]
    assert chosen[1].evidence["lessons_pending"] == 1
    assert "revised_kcs" not in chosen[1].evidence
    assert LESSON_B not in [c.module_id for c in chosen]
    evidence = chosen[0].evidence
    assert evidence["correct"] == 1 and evidence["attempts"] == 4
    assert evidence["quiz_module_id"] == QUIZ_1 and evidence["quiz_best_score"] == 0.5


def test_after_rereading_the_lesson_the_quiz_retry_is_recommended():
    learner = _failed_quiz_1(lesson_a_viewed_at=300)  # read again after failing

    chosen = recommend(learner, _inputs(learner))

    assert _codes(chosen)[0] == (QUIZ_1, schemas.RETRY_QUIZ)
    assert chosen[0].evidence["revised_kcs"] == ["KC-A"]
    assert LESSON_A not in [c.module_id for c in chosen]


def test_failed_quiz_without_a_kc_below_the_threshold_targets_its_weakest_kc():
    learner = _failed_quiz_1(lesson_a_viewed_at=50)
    learner.kcs["KC-A"] = KCState(4, 0.75, 0.7, 200, "no_current_concern")
    learner.kcs["KC-B"] = KCState(4, 0.60, 0.7, 200, "no_current_concern")

    assert _recommend(learner)[0] == (LESSON_B, schemas.REVISE_BEFORE_RETRY)


def _passed_quiz_1_weak_on_a(lesson_a_viewed_at):
    """Quiz 1 passed overall, but KC-A is an evidence-based concern."""
    return _learner(
        activities={
            OVERVIEW: ActivityState("completed", last_viewed=10),
            LESSON_A: ActivityState("completed", last_viewed=lesson_a_viewed_at),
            QUIZ_1: ActivityState("passed", last_viewed=100, last_attempt=200, attempt_count=1),
        },
        kcs={
            "KC-A": KCState(4, 0.5, 0.5, 200, "evidence_based_concern"),
            "KC-B": KCState(4, 1.0, 0.9, 200, "no_current_concern"),
        },
    )


def test_weak_kc_in_a_passed_quiz_gets_its_lesson_then_practice():
    not_reread = _passed_quiz_1_weak_on_a(lesson_a_viewed_at=50)
    assert _recommend(not_reread)[0] == (LESSON_A, schemas.REVIEW_WEAK_KC)

    reread = _passed_quiz_1_weak_on_a(lesson_a_viewed_at=300)
    assert _recommend(reread)[0] == (QUIZ_1, schemas.PRACTICE_WEAK_KC)
    # Either way the learner also keeps a way forward: Unit 1 is otherwise
    # done, so the next open activity is in Unit 2.
    assert (LESSON_C, schemas.CONTINUE_COURSE) in _recommend(reread)
    # A lesson whose quiz is passed is not offered as "next in the course".
    assert (LESSON_B, schemas.CONTINUE_COURSE) not in _recommend(reread)


def test_prediction_only_prepares_for_the_upcoming_quiz():
    """The model flags KC-A (next quiz) and KC-D (two units ahead)."""
    learner = _learner(
        phase=3,
        activities={OVERVIEW: ActivityState("completed", last_viewed=10)},
        kcs={
            "KC-A": KCState(0, None, 0.40, None, "prediction_based_concern"),
            "KC-B": KCState(0, None, 0.80, None, "insufficient_evidence"),
            "KC-D": KCState(0, None, 0.30, None, "prediction_based_concern"),
        },
    )

    chosen = recommend(learner, _inputs(learner), top_n=8)
    codes = dict(_codes(chosen))

    assert chosen[0].module_id == LESSON_A
    assert codes[LESSON_A] == schemas.PREPARE_PREDICTED_DIFFICULTY
    assert chosen[0].evidence["upcoming_quiz_module_id"] == QUIZ_1
    assert chosen[0].evidence["estimator_phase"] == 3
    # KC-D is predicted even weaker, but its quiz is not next: no prediction-based item.
    assert codes[LESSON_D] == schemas.CONTINUE_COURSE
    assert list(codes.values()).count(schemas.PREPARE_PREDICTED_DIFFICULTY) == 1


def test_a_lesson_already_read_is_not_recommended_as_preparation():
    learner = _learner(
        phase=3,
        activities={LESSON_A: ActivityState("completed", last_viewed=10)},
        kcs={"KC-A": KCState(0, None, 0.40, None, "prediction_based_concern")},
    )
    assert schemas.PREPARE_PREDICTED_DIFFICULTY not in dict(_recommend(learner)).values()


def test_a_learner_in_the_middle_of_the_course_continues_from_their_own_unit():
    learner = _learner(
        activities={
            LESSON_C: ActivityState("completed", last_viewed=500),
            QUIZ_2: ActivityState("passed", last_viewed=600, last_attempt=700, attempt_count=1),
        },
        kcs={"KC-C": KCState(4, 1.0, 0.9, 700, "no_current_concern")},
    )

    chosen = _recommend(learner)

    # Unit 2 is done: on to Unit 3, and only then back to the untouched Unit 1.
    assert chosen[:3] == [
        (LESSON_D, schemas.CONTINUE_COURSE),
        (QUIZ_3, schemas.CONTINUE_COURSE),
        (OVERVIEW, schemas.CONTINUE_COURSE),
    ]
    # The course-order baseline ignores the learner's position.
    baseline = recommend(learner, _inputs(learner), policy="course_order")
    assert baseline[0].module_id == OVERVIEW


def test_a_learner_who_finished_everything_gets_no_recommendation():
    done = {m: ActivityState("completed", last_viewed=m) for m in range(101, 109)}
    for quiz in (QUIZ_1, QUIZ_2, QUIZ_3):
        done[quiz] = ActivityState("passed", last_viewed=quiz, last_attempt=quiz)
    assert _recommend(_learner(activities=done)) == []


# ---------------------------------------------------------------- the ranking
def _candidate(module_id, code, priority=()):
    return Candidate(module_id=module_id, reason_code=code, priority=priority)


def test_remediation_is_capped_and_one_slot_is_kept_for_the_way_forward():
    remediation = [
        _candidate(module_id, schemas.REVIEW_WEAK_KC, (1, accuracy, module_id))
        for module_id, accuracy in [(1, 0.5), (2, 0.1), (3, 0.3), (4, 0.2), (5, 0.4)]
    ]
    forward = [_candidate(m, schemas.CONTINUE_COURSE) for m in (11, 12, 13)]

    chosen = select(remediation, forward, top_n=5, max_remediation=3)

    # The three weakest first, lowest accuracy first; then course order.
    assert [c.module_id for c in chosen] == [2, 4, 3, 11, 12]
    # Even with room for only two items, one of them is a way forward.
    assert [c.module_id for c in select(remediation, forward, 2, 3)] == [2, 11]
    # Without any forward item, remediation may use what it is allowed to.
    assert [c.module_id for c in select(remediation, [], 5, 3)] == [2, 4, 3]


def test_failed_quiz_items_outrank_other_weak_kcs_and_prepare_precedes_continue():
    remediation = [
        _candidate(1, schemas.REVIEW_WEAK_KC, (1, 0.1, 1)),
        _candidate(2, schemas.REVISE_BEFORE_RETRY, (0, 0.5, 2)),
    ]
    forward = [
        _candidate(11, schemas.CONTINUE_COURSE),
        _candidate(12, schemas.PREPARE_PREDICTED_DIFFICULTY),
        _candidate(13, schemas.CONTINUE_COURSE),
    ]

    assert [c.module_id for c in select(remediation, forward, 5, 3)] == [2, 1, 12, 11, 13]


def test_an_activity_appears_once_with_its_most_specific_reason():
    remediation = [_candidate(11, schemas.REVIEW_WEAK_KC, (1, 0.2, 11))]
    forward = [_candidate(11, schemas.CONTINUE_COURSE), _candidate(12, schemas.CONTINUE_COURSE)]

    chosen = select(remediation, forward, 5, 3)

    assert _codes(chosen) == [(11, schemas.REVIEW_WEAK_KC), (12, schemas.CONTINUE_COURSE)]


def test_prepare_never_takes_the_last_slot_from_plain_course_order():
    forward = [
        _candidate(11, schemas.PREPARE_PREDICTED_DIFFICULTY),
        _candidate(12, schemas.PREPARE_PREDICTED_DIFFICULTY),
        _candidate(13, schemas.CONTINUE_COURSE),
    ]
    assert [c.module_id for c in select([], forward, 2, 3)] == [11, 13]


# -------------------------------------------------------------- the baselines
def test_baselines_skip_completed_activities_and_are_deterministic():
    learner = _learner(activities={OVERVIEW: ActivityState("completed", last_viewed=1)})
    other = _learner("8", activities={LESSON_D: ActivityState("completed", last_viewed=1)})
    inputs = _inputs(learner, other)

    ordered = recommend(learner, inputs, "course_order", top_n=2)
    assert [c.module_id for c in ordered] == [LESSON_A, LESSON_B]

    counts = baselines.popularity_counts(inputs.learners)
    popular = recommend(learner, inputs, "popularity", 2, {"popularity": counts})
    assert popular[0].module_id == LESSON_D  # the only open activity anyone completed
    assert popular[0].evidence == {"completed_by_learners": 1}

    first = recommend(learner, inputs, "random", 3, {"seed": 1})
    again = recommend(learner, inputs, "random", 3, {"seed": 1})
    assert [c.module_id for c in first] == [c.module_id for c in again]
    assert OVERVIEW not in [c.module_id for c in first]
    with pytest.raises(ValueError, match="Unknown policy"):
        recommend(learner, inputs, "magic")


def test_content_based_ranks_by_similarity_to_wrongly_answered_content():
    course = build_course_graph(
        _catalog(), _kc_map(), KC_NAMES,
        {"KC-A": "grammar", "KC-B": "vocabulary", "KC-C": "grammar", "KC-D": "vocabulary"},
    )
    weak_a = _learner(kcs={"KC-A": KCState(attempts=4, observed_accuracy=0.25)})
    # Profile: grammar + Unit 1. Lesson A matches both; Quiz 1 (KC-A, KC-B)
    # is next (cosine 0.866); Lesson B matches the unit only (0.5).
    ranked = baselines.content_based(weak_a, course, 3)
    assert [c.module_id for c in ranked] == [LESSON_A, QUIZ_1, LESSON_B]
    assert ranked[0].reason_code == schemas.BASELINE_CONTENT_BASED
    assert ranked[0].evidence == {"similarity": 1.0, "fallback": None}

    # No answers: plain course order, and the output says so.
    fresh = baselines.content_based(_learner(), course, 2)
    assert [c.module_id for c in fresh] == [OVERVIEW, LESSON_A]
    assert fresh[0].evidence["fallback"] == "course_order"


def test_item_cf_uses_co_touched_activities_of_the_same_population_only():
    viewed = lambda: ActivityState("viewed", last_viewed=1)  # noqa: E731
    me = _learner("7", activities={LESSON_A: viewed()})
    peer = _learner("8", activities={LESSON_A: viewed(), LESSON_C: viewed()})
    peer2 = _learner(
        "9", activities={LESSON_A: viewed(), LESSON_C: viewed(), QUIZ_2: viewed()}
    )
    other = LearnerState("10", population="simulated", activities={LESSON_A: viewed()})
    inputs = _inputs(me, peer, peer2, other)
    context = {"similarities": baselines.item_similarities(inputs.learners)}

    # sim(A, C) = 2/sqrt(3*2) = 0.816 > sim(A, Q2) = 1/sqrt(3) = 0.577
    mine = recommend(me, inputs, "item_cf", 2, context)
    assert [c.module_id for c in mine] == [LESSON_C, QUIZ_2]
    assert mine[0].evidence == {"score": 0.8165, "fallback": None}
    # The simulated learner sees no real learner's co-occurrences: popularity
    # within its own population is all it gets.
    theirs = recommend(other, inputs, "item_cf", 2, context)
    assert theirs[0].module_id == LESSON_A
    assert theirs[0].evidence["fallback"] == "popularity"


def test_popularity_counts_only_the_learners_own_population():
    done = lambda: ActivityState("completed", last_viewed=1)  # noqa: E731
    me = _learner("7")
    peer = _learner("8", activities={LESSON_B: done()})
    simulated = [
        LearnerState(str(i), population="simulated", activities={LESSON_D: done()})
        for i in (10, 11, 12)
    ]
    inputs = _inputs(me, peer, *simulated)
    context = {"popularity": baselines.popularity_counts(inputs.learners)}

    # Three simulated completions of D must not outrank one real completion of B.
    mine = recommend(me, inputs, "popularity", 1, context)
    assert mine[0].module_id == LESSON_B
    assert mine[0].evidence == {"completed_by_learners": 1}
    newcomer = LearnerState("13", population="simulated")
    theirs = recommend(newcomer, inputs, "popularity", 1, context)
    assert theirs[0].module_id == LESSON_D
    assert theirs[0].evidence == {"completed_by_learners": 3}


# ---------------------------------------------------------- contract / output
def test_output_follows_the_contract_and_explains_every_row():
    learner = _failed_quiz_1(lesson_a_viewed_at=50)
    frame = recommend_all(_inputs(learner, _learner("8")), generated_at="2026-01-01T00:00:00+00:00")

    assert list(frame.columns) == schemas.RECOMMENDATION_COLUMNS
    mine = frame[frame["learner_id"].eq("7")]
    assert mine["rank"].tolist() == list(range(1, len(mine) + 1))
    first = mine.iloc[0]
    assert first["reason_code"] == schemas.REVISE_BEFORE_RETRY
    assert first["basis"] == schemas.BASIS_EVIDENCE
    assert first["kc_name"] == "Topic A" and first["activity_name"] == "Lesson: A"
    # Counts the learner can check, not a model probability.
    assert "1 of 4 questions" in first["reason_text"]
    assert "Unit 1 practice" in first["reason_text"]
    assert json.loads(first["evidence"])["observed_accuracy"] == 0.25
    assert frame["reason_text"].str.len().gt(0).all()
    assert frame["recommender_version"].eq(config.RECOMMENDER_VERSION).all()
    new = frame[frame["learner_id"].eq("8")]
    assert new.iloc[0]["reason_code"] == schemas.START_COURSE
    assert new["basis"].eq(schemas.BASIS_COURSE_ORDER).all()


def test_prediction_based_text_says_it_is_an_estimate():
    learner = _learner(
        phase=3, kcs={"KC-A": KCState(0, None, 0.40, None, "prediction_based_concern")}
    )
    frame = recommend_all(_inputs(learner))
    row = frame[frame["basis"].eq(schemas.BASIS_PREDICTION)].iloc[0]

    assert row["reason_text"].startswith("Estimate, not based on your answers")
    assert "0.4" not in row["reason_text"]  # the probability stays in the evidence
    assert json.loads(row["evidence"])["predicted_correctness"] == 0.4
    assert row["estimator_phase"] == 3


def test_contract_validation_rejects_broken_output():
    frame = recommend_all(_inputs(_learner()))

    duplicate_rank = frame.copy()
    duplicate_rank.loc[1, "rank"] = 1
    with pytest.raises(ValueError, match="same rank"):
        schemas.validate_recommendations(duplicate_rank)
    wrong_basis = frame.copy()
    wrong_basis.loc[0, "basis"] = schemas.BASIS_EVIDENCE
    with pytest.raises(ValueError, match="basis does not match"):
        schemas.validate_recommendations(wrong_basis)
    with pytest.raises(ValueError, match="not in the course"):
        schemas.validate_recommendations(frame, known_modules={1, 2})
    with pytest.raises(ValueError, match="more than 2"):
        schemas.validate_recommendations(frame, top_n=2)


# ------------------------------------------------------- generation from files
def _write_snapshot(tmp_path):
    """A learner-model build and its collector export, as files."""
    data_dir, profile_dir = tmp_path / "data", tmp_path / "profiles"
    data_dir.mkdir()
    profile_dir.mkdir()
    _catalog().to_csv(data_dir / "activity_catalog.csv", index=False)
    _kc_map().to_csv(data_dir / "kc_activity_map.csv", index=False)
    pd.DataFrame(
        {
            "learner_id": ["7", "8"],
            "source_population": ["moodle_collector", "simulated"],
            "knowledge_phase": [1, 3],
        }
    ).to_csv(profile_dir / "learner_profile.csv", index=False)
    iso = lambda seconds: pd.Timestamp(seconds, unit="s", tz="UTC").isoformat()  # noqa: E731
    pd.DataFrame(
        [
            dict(learner_id="8", course_module_id=LESSON_A, activity_type="page",
                 status="completed", last_viewed_at=iso(50), attempt_count=0),
            dict(learner_id="8", course_module_id=QUIZ_1, activity_type="quiz",
                 status="failed", last_viewed_at=iso(100), last_attempt_at=iso(200),
                 attempt_count=2, best_score=0.5, pass_fraction=0.7),
        ]
    ).to_csv(profile_dir / "learner_activity_state.csv", index=False)
    pd.DataFrame(
        [
            dict(learner_id="8", kc_id="KC-A", kc_name="Topic A", attempt_count=8,
                 observed_accuracy=0.25, predicted_correctness=0.4, last_attempt_at=iso(200)),
            dict(learner_id="8", kc_id="KC-B", kc_name="Topic B", attempt_count=8,
                 observed_accuracy=0.75, predicted_correctness=0.7, last_attempt_at=iso(200)),
        ]
    ).to_csv(profile_dir / "learner_kc_state.csv", index=False)
    pd.DataFrame(
        [
            dict(learner_id="8", entity_type="kc", entity_id="KC-A",
                 concern_status="evidence_based_concern"),
            dict(learner_id="8", entity_type="kc", entity_id="KC-B",
                 concern_status="no_current_concern"),
        ]
    ).to_csv(profile_dir / "weak_areas.csv", index=False)
    manifest = {
        "as_of": None,
        "mapping": {"version": "map-1"},
        "configuration": {"low_observed_accuracy_threshold": 0.6},
        "inputs": {
            name: {"sha256": sha256_file(data_dir / filename)}
            for name, filename in {
                "activity_catalog": "activity_catalog.csv",
                "kc_activity_map": "kc_activity_map.csv",
            }.items()
        },
    }
    (profile_dir / "build_manifest.json").write_text(json.dumps(manifest))
    return profile_dir, data_dir


def test_generate_reads_a_learner_model_build_and_writes_csv_and_manifest(tmp_path):
    profile_dir, data_dir = _write_snapshot(tmp_path)

    frame = generate(profile_dir, data_dir, tmp_path / "out")

    written = pd.read_csv(tmp_path / "out" / config.RECOMMENDATIONS_FILE, dtype={"learner_id": str})
    assert len(written) == len(frame)
    # The full queue: learner 7 (nothing done) has every recommendable activity in it,
    # then the failure branches of the three quizzes (2 + 1 + 1 lessons, 3 retries).
    branch = written["group"].eq(schemas.GROUP_CONTINGENCY)
    assert (written["learner_id"].eq("7") & ~branch).sum() == 8
    assert (written["learner_id"].eq("7") & branch).sum() == 7
    assert written.groupby("learner_id")["rank"].min().eq(1).all()
    simulated = written[written["learner_id"].eq("8")]
    assert simulated.iloc[0]["reason_code"] == schemas.REVISE_BEFORE_RETRY
    assert simulated.iloc[0]["course_module_id"] == LESSON_A
    assert "2 of 8 questions" in simulated.iloc[0]["reason_text"]
    assert simulated["source_population"].eq("simulated").all()
    real = written[written["learner_id"].eq("7")]
    assert real.iloc[0]["reason_code"] == schemas.START_COURSE
    assert real["source_population"].eq("moodle_collector").all()

    manifest = json.loads((tmp_path / "out" / config.MANIFEST_FILE).read_text())
    summary = manifest["summary"]
    assert manifest["configuration"]["full_queue"] is True
    assert summary["queue"]["rows"] == int((~branch).sum())
    assert summary["contingency"]["rows"] == int(branch.sum())
    assert summary["learners"] == summary["learners_with_recommendations"] == 2
    assert summary["learners_with_personalised_item"] == 1
    assert summary["by_population"]["moodle_collector"]["rows_by_basis"] == {"course_order": 5}
    assert manifest["configuration"]["low_observed_accuracy_threshold"] == 0.6
    assert "activity_catalog" in manifest["inputs"]


def test_profiles_built_from_another_course_export_are_refused(tmp_path):
    """After a course rebuild the module ids change; old profiles must not be used."""
    profile_dir, data_dir = _write_snapshot(tmp_path)
    rebuilt = _catalog()
    rebuilt["course_module_id"] += 1000
    rebuilt.to_csv(data_dir / "activity_catalog.csv", index=False)

    with pytest.raises(ValueError, match="not the file the learner profiles were built from"):
        load_inputs(profile_dir, data_dir)
    with pytest.raises(FileNotFoundError, match="No learner-model build"):
        load_inputs(tmp_path / "nowhere", data_dir)


# ------------------------------------------------------------------ the full queue
def _states_for_queue_tests():
    """Learners in different situations: new, failed quiz, weak KCs, mid-course."""
    iso = 1_000
    new = _learner("1")
    failed = _learner(
        "2",
        activities={
            LESSON_A: ActivityState("completed", last_viewed=iso),
            QUIZ_1: ActivityState("failed", last_viewed=iso + 5, last_attempt=iso + 10,
                                  attempt_count=1, best_score=0.4, pass_fraction=0.7),
        },
        kcs={
            "KC-A": KCState(4, 0.25, 0.4, iso + 10, "evidence_based_concern"),
            "KC-B": KCState(4, 0.5, 0.5, iso + 10, "evidence_based_concern"),
        },
    )
    weak_everywhere = _learner(
        "3",
        phase=3,
        activities={
            QUIZ_1: ActivityState("passed", last_attempt=iso, attempt_count=1, best_score=0.8),
            QUIZ_2: ActivityState("passed", last_attempt=iso + 50, attempt_count=1, best_score=0.7),
        },
        kcs={
            "KC-A": KCState(4, 0.5, 0.5, iso, "evidence_based_concern"),
            "KC-B": KCState(4, 0.5, 0.5, iso, "evidence_based_concern"),
            "KC-C": KCState(4, 0.5, 0.5, iso + 50, "evidence_based_concern"),
            "KC-D": KCState(0, None, 0.3, None, "prediction_based_concern"),
        },
    )
    return [new, failed, weak_everywhere]


def _queue(learner, top_n=5):
    course = _course()
    return order_queue(
        remediation_candidates(learner, course, 0.6),
        forward_candidates(learner, course, config.MAX_PREPARE),
        top_n,
        config.MAX_REMEDIATION,
    )


@pytest.mark.parametrize("top_n", [1, 2, 3, 5])
def test_the_queue_starts_with_exactly_the_top_n_list(top_n):
    for learner in _states_for_queue_tests():
        queue = _queue(learner, top_n)
        head = recommend(learner, _inputs(learner), top_n=top_n)
        assert _codes(queue[:top_n]) == _codes(head)
        assert _codes(window(queue, set(), top_n, config.MAX_REMEDIATION)) == _codes(head)
        assert len({c.module_id for c in queue}) == len(queue)  # each activity once


def test_the_queue_covers_every_open_activity():
    new = _states_for_queue_tests()[0]
    queue = _queue(new)
    assert [c.module_id for c in queue] == [a.module_id for a in _course().activities]


def test_working_through_the_list_never_empties_it_before_the_course_is_done():
    """The block's invariant: something to do is shown until nothing is left."""
    for learner in _states_for_queue_tests():
        queue = _queue(learner)
        done = set()
        while True:
            shown = window(queue, done, config.TOP_N, config.MAX_REMEDIATION)
            left = [c for c in queue if c.module_id not in done]
            assert bool(shown) == bool(left)
            if not shown:
                break
            remediation = sum(ranking_group(c) == schemas.GROUP_REMEDIATION for c in shown)
            assert remediation <= config.MAX_REMEDIATION
            forward_left = [c for c in left if ranking_group(c) != schemas.GROUP_REMEDIATION]
            if forward_left:
                assert any(ranking_group(c) != schemas.GROUP_REMEDIATION for c in shown)
            done.add(shown[0].module_id)  # the learner does the first item


def ranking_group(candidate):
    return schemas.REASON_GROUPS[candidate.reason_code]


def test_a_finished_course_with_weak_topics_still_offers_consolidation():
    done = {m: ActivityState("completed", last_viewed=1) for m in (OVERVIEW, LESSON_A, LESSON_B, LESSON_C, LESSON_D)}
    done.update({q: ActivityState("passed", last_attempt=2, attempt_count=1, best_score=0.8) for q in (QUIZ_1, QUIZ_2, QUIZ_3)})
    learner = _learner("4", activities=done, kcs={"KC-B": KCState(5, 0.4, 0.5, 2, "evidence_based_concern")})
    # Lesson B was last read before the last answers on KC-B: reread it first.
    queue = _queue(learner)
    assert _codes(queue) == [(LESSON_B, schemas.REVIEW_WEAK_KC)]
    # Once it is reread (a refresh sees that), practise in its quiz.
    done[LESSON_B] = ActivityState("completed", last_viewed=3)
    assert _codes(_queue(learner)) == [(QUIZ_1, schemas.PRACTICE_WEAK_KC)]


def test_queue_rows_carry_their_group_and_full_length(tmp_path):
    learners = _states_for_queue_tests()
    frame = recommend_all(_inputs(*learners), full_queue=True)
    branch = frame["trigger_cmid"].notna()
    assert frame.loc[~branch, "group"].eq(frame.loc[~branch, "reason_code"].map(schemas.REASON_GROUPS)).all()
    assert frame.loc[branch, "group"].eq(schemas.GROUP_CONTINGENCY).all()
    assert frame.groupby("learner_id").size().gt(config.TOP_N).any()
    top = recommend_all(_inputs(*learners))
    assert frame[frame["rank"].le(config.TOP_N)][["learner_id", "rank", "course_module_id"]].reset_index(
        drop=True
    ).equals(top[["learner_id", "rank", "course_module_id"]])


def test_the_shared_window_fixture_matches_the_python_rule():
    """The Moodle block is tested against the same file (recommendation_window_test.php)."""
    from tests.window_fixture import FIXTURE, expected

    cases = json.loads(FIXTURE.read_text())["cases"]
    assert len(cases) > 20
    for case in cases:
        assert expected(case) == case["expected"], (
            f"{case['name']}: the slot rules changed; run `python -m tests.window_fixture` "
            "and check the PHP window still agrees"
        )


def test_the_moodle_plugin_has_the_same_window_fixture():
    from tests.window_fixture import FIXTURE, PLUGIN_FIXTURE

    if not PLUGIN_FIXTURE.parent.parent.parent.is_dir():
        pytest.skip("the moodle submodule is not checked out")
    assert PLUGIN_FIXTURE.read_text() == FIXTURE.read_text(), "run `python -m tests.window_fixture`"


def test_the_php_window_picks_what_the_python_window_picks():
    """Runs recommendation_window::select over the shared cases inside the Moodle container."""
    import shutil
    import subprocess

    container = "moodle-docker-webserver-1"
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    probe = subprocess.run(["docker", "exec", container, "true"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"container {container} is not running")
    script = (
        "require '/var/www/html/public/local/microlearning/classes/local/recommendation_window.php';"
        "$cases = json_decode(file_get_contents('/var/www/html/public/local/microlearning/tests/fixtures/"
        "recommendation_window.json'), true)['cases'];"
        "$out = [];"
        "$w = '\\local_microlearning\\local\\recommendation_window';"
        "foreach ($cases as $c) { $out[] = $w::select($w::effective_queue($c['queue'], $c['active']),"
        " $c['done'], $c['top_n'], $c['max_remediation']); }"
        "echo json_encode($out);"
    )
    result = subprocess.run(["docker", "exec", container, "php", "-r", script],
                            capture_output=True, text=True, check=True)
    from tests.window_fixture import FIXTURE

    cases = json.loads(FIXTURE.read_text())["cases"]
    assert json.loads(result.stdout) == [case["expected"] for case in cases]
