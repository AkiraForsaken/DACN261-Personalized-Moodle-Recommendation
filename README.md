# Personalized Microlearning Recommendation Using Moodle Learning Analytics

For every learner of a Moodle course, this system estimates what they know,
summarises it as a learner profile, and recommends their next activities with
a reason they can check. Students see their recommendations in a Moodle block;
teachers see each learner's knowledge profile. Everything is computed from
**that course's own data**: no external dataset, model or learner record is
used for a course's learners.

## Two pipelines that never mix

|          | Benchmark experiment                       | Course pipeline                                                                             |
| -------- | ------------------------------------------ | ------------------------------------------------------------------------------------------- |
| Question | Which knowledge-tracing model works best?  | What does each learner of this course know, and what should they do next?                  |
| Data     | A public English learning dataset          | One Moodle course (its real learners, or simulated learners for testing)                   |
| Code     | `knowledge_tracing/` with `english_kt`     | `collector/` → `knowledge_engine/` → `learner_model/` → `recommender/` → `moodle_sync/`     |
| Result   | A model comparison that justifies the choice of model | Learner profiles and recommendations inside Moodle                              |

A knowledge-tracing model is tied to the questions and skills it was trained
on, so a benchmark model cannot score a Moodle course. The benchmark only
justifies *which kind* of model the course pipeline trains for itself.

## How a course flows through the system

```
Moodle database
   │  collector          read-only SQL export: learners, activities, quiz answers, events, completion
   ▼
data/                    the course's Knowledge Components (KCs) come from its question bank
   │  knowledge_engine   probability that each learner answers each KC correctly
   ▼
knowledge/
   │  learner_model      profile per learner: knowledge levels, skills, weak areas, behaviour, engagement, history
   ▼
learner_model/
   │  recommender        ranked queue of next activities per learner, each with a reason and its basis
   ▼
recommendations/
   │  moodle_sync / api  versioned bundle with checksums, imported by the Moodle plugins
   ▼
Moodle: local_microlearning (storage, teacher pages, refresh requests) + block_microlearning (students)
```

Each stage reads only the previous stages' files and records what it read
(hashes, cutoff time, model, mapping version) in a manifest, so any output can
be traced back to the data it came from.

### 1. Knowledge Components come from the course

A **Knowledge Component (KC)** is one skill a question tests. A course defines
its KCs through two Moodle conventions, which the collector reads:

- each question category with an **ID number** is a KC (`kc_id`); its parent
  category is the KC's **dimension** (grammar, vocabulary, ...);
- optionally, an activity whose **ID number** is a `kc_id` is that KC's
  learning resource, so a weak KC can lead straight to its lesson.

Details: [learner_model/docs/mapping_documentation.md](learner_model/docs/mapping_documentation.md).

### 2. Knowledge estimation in three phases

A new course has no data to train a model on, so estimation grows with the
course:

1. **Cold start.** Bayesian Knowledge Tracing (BKT) with fixed priors. Every
   enrolled learner has a profile from day one; an unanswered KC holds a
   prior, never a score.
2. **Training.** Once enough learners have answered enough questions
   (`knowledge-engine.py status`), a deep knowledge-tracing model (AKT) is
   trained on the course's own answers. Training is a deliberate step, never
   automatic.
3. **Personalised.** The deep model takes over only if it beats BKT on
   held-out learners of the same course. Otherwise BKT stays in charge.

A model is applied only to the population it was trained on: a model trained
on simulated learners never scores a real learner.
[knowledge_engine/README.md](knowledge_engine/README.md)

### 3. Profiles and recommendations

The learner model turns knowledge states and activity logs into a profile:
per-KC **knowledge levels** from the learner's own answers, skill and
dimension summaries, weak areas (with the evidence behind each), behaviour,
engagement and quiz history. [learner_model/README.md](learner_model/README.md)

The recommender turns the profile into a **queue** of every open activity,
remediation first. Each item names its reason ("2 of 8 questions answered
correctly") and its **basis**: the learner's answers, a model estimate, or
plain course order. Precomputed branches handle "if this quiz is failed".
[recommender/README.md](recommender/README.md)

### 4. Delivery and refresh in Moodle

The plugins store the latest snapshot. The student block shows the next five
items of the learner's queue that they have not dealt with yet, so the list
refills as they work. When a learner finishes their list, fails a quiz or
enrols, Moodle asks the **engine service** to recompute that learner only
(about a second), downloads the result and imports it; a nightly task rebuilds
the whole course. The engine reads Moodle's database but never writes to it.
[moodle_sync/README.md](moodle_sync/README.md), [api/README.md](api/README.md)

## One workspace per course

Everything the course pipeline reads and writes lives in the course's
**workspace**, so one engine can serve several courses and Moodle sites:

```
courses/<site>/<course_id>/        site: $MICROLEARNING_SITE or "local"; root: $MICROLEARNING_HOME or ./courses
    course.json  data/  knowledge/  kt/  learner_model/  recommendations/  evaluation/  bundle/  refresh/  worker/
```

`kt/` holds the course's own deep model; `knowledge/active_model.json` records
which estimator is in charge. `python -m course_workspace show --course <id>`
prints a course's folders; `list` shows all workspaces.

## Running it

```bash
pip install -r requirements.txt           # pipeline, collector, engine service
pip install -r requirements-kt.txt        # PyTorch + pyKT: training and the deep model
export MOODLE_COURSE_ID=<id>              # every command also takes --course <id>
export MOODLE_DOCKER_DBPASS=...           # database settings: collector/README.md
./run_pipeline.sh --course <id>           # collector → ... → import into Moodle
./run_pipeline.sh --course <id> --no-push # stop after building the bundle
python -m pytest tests -q
```

Two virtualenvs are used because the MariaDB driver and PyTorch need different
Python versions: `.venv` for the collector, the refresh worker and the engine
service; `.venv-kt` for training, estimation with a deep model, the model
stages and the offline evaluation. The service and the worker call `.venv-kt`
themselves.

### Entry points

Each stage has a thin script in the repository root; the logic lives in the
package of the same name (`python -m <package>` is equivalent). Every script
accepts `--help` and `--course <id>`.

| Script                                                                                            | Does                                                   |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `moodle-collector.py [--learners ID,...]`                                                         | export the course from Moodle's database               |
| `knowledge-engine.py status\|estimate\|train\|simulate\|oracle`                                   | knowledge states, training and promotion gate          |
| `learner-model.py`                                                                                | learner profiles                                       |
| `recommend.py generate\|evaluate`                                                                 | recommendation queues; offline replay against baselines |
| `moodle-sync.py bundle\|push\|refresh\|worker\|stages`                                            | bundle, import, per-learner refresh                    |
| `engine-api.py`                                                                                   | the engine service Moodle calls                        |
| `moodle-setup.py build-course\|simulate-learners\|annotate-plan\|remove-simulated\|check-content` | scripted test course and simulated learners            |
| `./run_pipeline.sh --course ID [--no-push]`                                                       | collector to Moodle in one command                     |
| `python -m course_workspace show\|list\|migrate`                                                  | course workspaces                                      |

### Testing without real learners

`moodle-setup.py` builds a complete English test course in a local Moodle and
injects simulated learners through Moodle's own APIs, so every stage can be
exercised on data that went through Moodle
([moodle_setup/README.md](moodle_setup/README.md)). The knowledge engine can
also simulate answers offline, into a separate folder and workspace:

```bash
python knowledge-engine.py simulate --output-dir data/simulated
python knowledge-engine.py train    --site offline --course <id> --data-dir data/simulated
python knowledge-engine.py estimate --site offline --course <id> --data-dir data/simulated
```

Simulated learners are labelled per learner and throughout; results on them
show that the pipeline works, not that it helps real learners.

## Packages

| Package              | Role                                                                  | Docs                                  |
| -------------------- | --------------------------------------------------------------------- | ------------------------------------- |
| `course_workspace/`  | One folder per course                                                 | module docstring                      |
| `collector/`         | Moodle database → CSV exports, including the KC catalog and mappings  | [README](collector/README.md)         |
| `knowledge_engine/`  | Learner-by-KC knowledge states, phases and promotion gate, simulator  | [README](knowledge_engine/README.md)  |
| `learner_model/`     | Learner profiles, knowledge levels and their components               | [README](learner_model/README.md)     |
| `recommender/`       | Explainable recommendation queues and their offline replay            | [README](recommender/README.md)       |
| `moodle_sync/`       | Bundle, import, per-learner refresh; the two Moodle plugins           | [README](moodle_sync/README.md)       |
| `api/`               | Engine service Moodle calls for refreshes, full runs and bundles      | [README](api/README.md)               |
| `knowledge_tracing/` | Deep-KT toolkit: benchmark experiment and course-model training       | [README](knowledge_tracing/README.md) |
| `moodle_setup/`      | Scripted test course and simulated learners in a local Moodle         | [README](moodle_setup/README.md)      |

Results and verification on the test course:
[docs/test_course_results.md](docs/test_course_results.md).

The Moodle plugins live in the `moodle` submodule (a Moodle 5.2 fork); the
local Docker setup in `moodle-docker`. An earlier topic-level recommender, its
HTTP API and its plugins were removed; the plugins remain in the Moodle fork's
history (commit `2dfbc4dd519`).
