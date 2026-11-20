# Recommender

Turns the learner model into a short, ranked list of next activities per
learner. Every recommendation carries a reason code, a sentence for the
learner, and its **basis**: the learner's own answers, a model estimate, or
plain course order.

```bash
python knowledge-engine.py estimate --course 10   # knowledge states
python learner-model.py --course 10               # learner profiles
python recommend.py generate --course 10          # -> courses/local/10/recommendations/
python recommend.py evaluate --course 10          # offline replay (needs requirements-kt.txt
                                                  #    when a deep model is promoted)
```

`recommend.py` in the repository root is the same as `python -m recommender`.

## Inputs

| File | From | Used for |
| --- | --- | --- |
| `weak_areas.csv` | learner model | which KCs are a concern, and on what basis |
| `learner_kc_state.csv` | learner model | answers, observed accuracy, prediction, last answer time |
| `learner_activity_state.csv` | learner model | failed and passed quizzes, lesson views and their times |
| `learner_profile.csv` | learner model | population, estimator phase |
| `build_manifest.json` | learner model | cutoff, mapping version, thresholds, input hashes |
| `activity_catalog.csv`, `kc_activity_map.csv` | collector | course order; KC to lesson and to practice quiz |

Learner state comes only from the learner model. Course structure is not
learner state, so it is read from the collector export; `inputs.py` checks its
SHA-256 against the hashes in `build_manifest.json` and refuses profiles built
from another export (after a course rebuild every course-module id changes).

## Rules

| Reason code | Fires when | Recommends | Basis |
| --- | --- | --- | --- |
| `revise_before_retry` | a quiz is currently failed; the lesson of one of its weak KCs was not read since the last attempt | that lesson | evidence |
| `retry_quiz` | a quiz is failed and nothing is left to revise for it | the quiz | evidence |
| `revisit_prerequisite` | a quiz was failed again although a weak KC's lesson was read between the last two attempts | up to 2 lessons of the same dimension in earlier units, weak or not assessed, nearest unit first | evidence |
| `review_weak_kc` | evidence-based concern on a KC outside a failed quiz; lesson not read since the last answer | lesson | evidence |
| `practice_weak_kc` | same, lesson read since | the practice quiz | evidence |
| `prepare_predicted_difficulty` | prediction-based concern on a KC of the quiz the learner is about to take; lesson not read | lesson | prediction |
| `continue_course` | always | next open activity, from the learner's current unit | course order |
| `start_course` | the learner has done nothing yet | first activity | course order |

A failed quiz's weak KCs are those answered below the learner model's
accuracy threshold (0.60), or its single weakest KC if none is. "Current unit"
is the unit of the learner's most recent activity; the scan wraps around the
course. A lesson whose practice quiz is already passed is not offered as
"next".

**Failing again after revising.** "Failed after revising" = the latest
attempt failed and at least one weak-KC lesson of the quiz was viewed between
the previous attempt and the latest one (attempt history from
`learner_quiz_attempts.csv`, every view from `learning_events.csv`, both before
the cutoff). The revise/retry loop continues, and earlier-unit lessons are
added right after the weakest lesson (or before the retry). "Same dimension,
earlier unit = prerequisite" is a **course-design assumption**
(`config.PREREQUISITE_RELATION`), not a relation learned from data. A quiz of
Unit 1 has no earlier unit, so only the loop applies there.

Two deliberate restrictions:

- **Predictions only prepare for the upcoming quiz.** A deep model can flag
  most of the course for one learner. Sending a Unit 1 learner to Unit 7 on
  that basis would be neither explainable nor useful.
- **A Phase 1 prior never produces a recommendation.** The learner model
  raises no prediction-based concern from the cold-start prior that all new
  learners share, so a new learner gets `start_course` / `continue_course`.

## Ranking

`generate` writes each learner's **full queue**: every candidate, so every
open activity of the course. Its head (rank 1..5) is the top 5 below; the
Moodle block shows the first five queue items the learner has not dealt with
yet, picked with the same slot rules (`ranking.window`, mirrored in PHP by
`recommendation_window::select`; both are tested against
`tests/fixtures/recommendation_window.json`, regenerated with
`python -m tests.window_fixture`). So the list refills as the learner works
and is empty only when the course is complete; a per-learner refresh (see
[moodle_sync/README.md](../moodle_sync/README.md)) then recomputes the queue.
`evaluate` scores the top 5 only, so its numbers did not change with the queue.

**Failure branches (`group = contingency`).** After the queue come
precomputed branches, one per quiz not yet passed: "if this quiz is failed",
its KC lessons weakest first (observed accuracy, else the model estimate),
then the quiz again (`retry_quiz`). For a quiz that is already failed the
branch is "if it is failed again": the weakest lesson, earlier-unit lessons,
the other weak lessons, the retry. Each row names its quiz in `trigger_cmid`.
Moodle activates a branch as soon as the quiz is failed (completion state
"failed" with an attempt after the learner's refresh): `ranking.effective_queue`
/ `recommendation_window::effective_queue` put its rows first, as remediation,
and drop the quiz's plain row. So a failed quiz turns the list back to its
lessons at once, even when the engine is down; the next refresh replaces the
branch with a list built from the real attempt. Branch lessons count as done
when viewed after the failing attempt, the retry when the quiz is passed.

The top 5 has a fixed shape (`ranking.py`), not a global score:

1. up to 3 remediation items: failed-quiz items first, then lowest accuracy;
2. up to 2 `prepare` lessons;
3. the next activities in course order.

One slot is always kept for a way forward, so a learner with many weak KCs is
not trapped in remediation and the list is empty only when the course has no
open activity. An activity appears once, with its most specific reason.

## Output

`recommendations.csv`, one row per learner and rank (the full queue, then the
failure branches): `learner_id, rank, group, trigger_cmid, course_module_id, activity_type, activity_name, kc_id, kc_name, reason_code,
reason_text, basis, evidence, policy, generated_at, as_of, estimator_phase,
source_population, mapping_version, recommender_version`.

`reason_text` uses counts a learner can check ("2 of 8 questions"), never a
probability, and a prediction-based text starts with "Estimate, not based on
your answers". `evidence` is a JSON object with the numbers, for teachers.
`recommendation_manifest.json` records input hashes, settings, and how many
learners have at least one evidence- or prediction-based item.

## Evaluation

`python -m recommender evaluate --cutoffs 6 --horizon-days 7` replays the
pipeline at past cutoffs (knowledge states up to T-1, learner model strictly
before T) and compares `rules` with three baselines: `course_order` (first
open activities from the top of the course), `popularity`, `random`. It
writes `metrics.csv` (per cutoff), `summary.csv`, `report.md`.

| Metric | Shows | Caveat |
| --- | --- | --- |
| `hit_at_n`, `mrr`, `first_next_hit` | agreement with what learners did next | the simulator makes learners revise after failing and move forward in order; rules built on the same habits agree by construction |
| later accuracy on flagged vs other KCs | whether flagged KCs really were weak | flagged KCs are selected for low past accuracy; part of the gap is KC difficulty |
| shares by basis | how much is personalised | most rows are course order while most learner x KC states are cold start |
| `stability_jaccard` | overlap of a learner's list between cutoffs | |

Leakage controls: the `bkt` variant has no trained weights and is scored on
every learner; the `active` variant (a promoted deep model, trained on the
whole period) is scored only on its held-out test learners.

Result on the 300 simulated learners of course 10 (seed 7, 6 cutoffs, 7 days,
top 5; 2026-11-16):

| Estimator | Policy | hit@5 | MRR |
| --- | --- | --- | --- |
| BKT, all learners | rules | 0.46 | 0.28 |
| | popularity | 0.30 | 0.15 |
| | random | 0.13 | 0.06 |
| | course order | 0.07 | 0.06 |
| AKT, 57 held-out | rules | 0.51 | 0.33 |
| | popularity | 0.32 | 0.16 |

Later accuracy on KCs flagged for remediation was 0.70 against 0.86 on the
learners' other answered KCs (BKT; 4,683 answers), and 0.51 against 0.69 for
prediction-flagged KCs (AKT, held-out learners; 272 answers). About 61 % of
learners had at least one personalised item; 70 to 77 % of rows were course
order. These numbers describe the simulator.

## Limits

- Rules, thresholds and slot sizes are design choices, not calibrated.
- The prerequisite relation (same dimension, earlier unit) is an assumption
  about this course's design. Weak KCs of a failed quiz are judged on all
  answers to that KC so far, not on the last attempt alone.
- Nobody followed these recommendations: the replay measures agreement and
  targeting, not learning gain. Any effect of lessons on simulated answers is
  a generator assumption (`--lesson-gain`).
- The recommender does not use recency or spacing. Simulated activity is
  backdated, so "days since last activity" is not meaningful there.

## Files

| File | Role |
| --- | --- |
| `config.py` | paths and the tunable numbers |
| `schemas.py` | output columns, reason codes, validation |
| `inputs.py` | loads and cross-checks one snapshot |
| `course.py` | ordered activities and KC links |
| `rules.py` | one rule per reason code, producing candidates |
| `ranking.py` | slots, order, de-duplication |
| `reasons.py` | learner-facing sentences |
| `baselines.py` | comparison policies |
| `generate.py` | orchestration, CSV and manifest |
| `evaluate.py` | offline replay and metrics |
