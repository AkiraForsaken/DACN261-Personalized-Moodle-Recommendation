# Learner model

Builds one **learner profile** per enrolled learner of a course: what the
learner knows, where they struggle, and how they work. It joins two sources:

- knowledge states from `knowledge_engine` (per learner and KC);
- behaviour, engagement and history from the collector's export (events,
  sessions, quiz attempts, completion).

The profile is the single description of a learner that the recommender and
the teacher pages read. No benchmark dataset is an input.

| Component | Answers the question | Main output |
| --- | --- | --- |
| knowledge | how likely is a correct answer on each KC, and on what evidence? | `learner_kc_state.csv` |
| knowledge levels | what do the learner's own answers say per KC? | `knowledge_level` in `weak_areas.csv` |
| skills / dimensions | how does the learner do per unit x dimension, and per dimension? | `learner_skill_summary.csv`, `learner_dimension_summary.csv` |
| weak areas | which KCs are a concern, and is that based on answers or on a prediction? | `weak_areas.csv` |
| activity state | which quizzes are passed or failed, which lessons were viewed, when? | `learner_activity_state.csv` |
| behaviour, engagement, history | how often, how long and how regularly does the learner work? | `learner_behavior.csv`, `learner_engagement.csv`, `learner_history.csv` |
| profile | one summary row per learner: population, estimator phase, component status | `learner_profile.csv` |
| quiz history | every finished attempt, score and pass mark | `learner_quiz_attempts.csv` |

Every column is defined in [the data contract](docs/learner_model_data_contract.md)
and [component definitions](docs/component_definitions.md).

## Build

From the repository root:

```bash
python moodle-collector.py --course <id>            # Moodle -> courses/<site>/<id>/data/
python knowledge-engine.py estimate --course <id>   # -> learner-by-KC knowledge states
python learner-model.py --course <id>               # -> courses/<site>/<id>/learner_model/
```

`learner-model.py` is the same as `python -m learner_model.builder`.

Options: `--data-dir`, `--knowledge-states`, `--output-dir`, `--as-of` (Unix
seconds or a timezone-aware ISO datetime) and `--generated-at` (pin the
generation time for byte-identical rebuilds).

The knowledge-state file is optional. Without it, profiles are still built and
the knowledge, skills and weak-area components are `unavailable`.

## What a new learner gets

A learner who is enrolled but has done nothing still has a profile:

- knowledge: one row per course KC, `cold_start` (the shared prior),
  `knowledge_status = cold_start_only`
- behavior, engagement, history: `no_observations`
- weak areas: `insufficient_evidence` for every KC, never a fabricated concern

See [the data contract](docs/learner_model_data_contract.md),
[component definitions](docs/component_definitions.md) and
[KC and skill mapping](docs/mapping_documentation.md).

## Knowledge levels and quiz history

Each learner x KC also gets a **knowledge level** (`knowledge_levels.py`,
column `knowledge_level` of `weak_areas.csv`), from the learner's answers only:

| Level          | Rule                                                               |
| -------------- | ------------------------------------------------------------------ |
| `not_assessed` | no answer                                                          |
| `started`      | 1-2 answers: too few to judge                                      |
| `weak`         | >= 3 answers, accuracy < 0.60 (exactly the evidence-based concern) |
| `developing`   | >= 3 answers, 0.60 <= accuracy < 0.80                              |
| `strong`       | >= 3 answers, accuracy >= 0.80                                     |

It ignores predictions, so it means the same under BKT and the deep model.
Skill and dimension summaries carry the number of KCs per level, and
`learner_quiz_attempts.csv` lists every finished quiz attempt before the
cutoff (score, pass mark, passed). These feed the teacher's charts. The
thresholds are configuration (`config.py`), not calibrated: the levels are
display categories, not mastery.

## Not in this package

Recommendation ([recommender/](../recommender/README.md)) and delivery to
Moodle ([moodle_sync/](../moodle_sync/README.md)) read the outputs above; this
package does not depend on them.
