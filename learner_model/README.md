# Learner model

Builds one learner profile per enrolled learner of a Moodle course, from that
course's own data: knowledge states from `knowledge_engine`, and behavior,
engagement and history from the Moodle collector. No benchmark dataset is an
input.

## Build

From the repository root:

```bash
python moodle-collector.py --course 10            # Moodle -> courses/local/10/data/
python knowledge-engine.py estimate --course 10   # -> learner-by-KC knowledge states
python learner-model.py --course 10               # -> courses/local/10/learner_model/
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
