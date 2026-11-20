# Personalized Microlearning Recommendation Using Moodle Learning Analytics

Builds a learner model (knowledge, skills, weak areas, behavior, engagement,
history) for the learners of a Moodle course, from that course's own data,
recommends each learner's next activities with reasons, and shows both in
Moodle: recommendations to students, knowledge profiles to teachers.

## Two distinct pipelines

|          | Benchmark experiment                      | Course pipeline                                                                             |
| -------- | ----------------------------------------- | ------------------------------------------------------------------------------------------- |
| Question | Which knowledge-tracing model works best? | What does each of our learners know, and what should they do next?                          |
| Data     | Public English Learning Events dataset    | The Moodle course (or simulated learners for it)                                            |
| Code     | `knowledge_tracing/` with `english_kt`    | `collector/` -> `knowledge_engine/` -> `learner_model/` -> `recommender/` -> `moodle_sync/` |
| Result   | Model comparison for the thesis           | Learner profiles and recommendations in Moodle                                              |

The benchmark justifies the choice of model. Its weights, KC IDs and learner
states are never used for Moodle learners.

## One folder per course

Everything the course pipeline reads and writes lives in the course's
**workspace**, so the engine can serve several courses (and several Moodle
sites) side by side:

```
courses/<site>/<course_id>/        site: $MICROLEARNING_SITE or "local"; root: $MICROLEARNING_HOME or ./courses
    course.json  data/  knowledge/  kt/  learner_model/  recommendations/  evaluation/  bundle/  refresh/  worker/
```

`kt/` holds the course's own deep-KT model; `knowledge/active_model.json` the
decision which estimator is in charge. Every command takes `--course <id>`
(default `$MOODLE_COURSE_ID`). `python -m course_workspace show --course 10`
prints the folders; `list` shows every course workspace. Details:
`course_workspace/__init__.py`.

## Course pipeline

```
Moodle DB --collector--> data/ --knowledge_engine--> knowledge/ --learner_model--> learner_model/
          --recommender--> recommendations/ --moodle_sync--> bundle/ --> Moodle plugins
```

```bash
pip install -r requirements.txt
export MOODLE_COURSE_ID=10 MOODLE_DOCKER_DBPASS=...   # see collector/config.py
./run_pipeline.sh --course 10       # collector -> ... -> import into Moodle
pytest tests -q
```

### Entry points

Each stage has a thin script in the repository root; the logic lives in the
package of the same name. `python -m <package>` is equivalent.

| Script                                                  | Stage                                   |
| ------------------------------------------------------- | --------------------------------------- |
| `moodle-collector.py [--learners 7,12]`                 | Moodle database -> `data/`              |
| `knowledge-engine.py status\|estimate\|train\|simulate\|oracle` | knowledge states, training, gate, oracle AUC |
| `learner-model.py`                                      | learner profiles                        |
| `recommend.py generate\|evaluate`                       | recommendation queues, offline replay   |
| `moodle-sync.py bundle\|push\|refresh\|worker\|stages`  | import into Moodle, per-learner refresh |
| `engine-api.py`                                         | the engine service Moodle calls ([api/README.md](api/README.md)) |
| `moodle-setup.py build-course\|simulate-learners\|annotate-plan\|remove-simulated\|check-content` | test course and simulated learners |
| `./run_pipeline.sh --course ID [--no-push]`             | collector to Moodle in one command      |
| `python -m course_workspace show\|list\|migrate`        | the course folders                      |

Every script accepts `--help`. The collector and the refresh worker run in
`.venv` (MariaDB driver); `knowledge-engine.py train`, `estimate` with a
promoted deep model, the model stages and `recommend.py evaluate` need
`.venv-kt` (PyTorch). The worker and the engine service (`.venv`) call
`.venv-kt` themselves for the model stages.

Knowledge estimation moves through three phases:

1. **Cold start.** Item-level BKT with fixed priors. Every enrolled learner has
   a profile from day one; unanswered KCs hold a prior, not a score.
2. **Training.** Once enough answers exist (`knowledge-engine.py status`),
   `knowledge-engine.py train` trains a deep KT model on the course's answers.
   Needs `pip install -r requirements-kt.txt`.
3. **Personalised.** The deep model takes over only if it beats BKT on
   held-out learners of the course. Otherwise BKT stays in charge.

To exercise phases 2 and 3 without Moodle, simulate offline into a separate
data folder; the trained model lands in the workspace you name:

```bash
python knowledge-engine.py simulate --output-dir data/simulated
python knowledge-engine.py train    --site offline --course 2 --data-dir data/simulated
python knowledge-engine.py estimate --site offline --course 2 --data-dir data/simulated
```

Simulated results are labelled `simulated` throughout and show only that the
pipeline works.

## Recommendations and refresh

The recommender writes each learner's **full queue**: every open activity,
remediation first, with the familiar top 5 at its head. The student block in
Moodle shows the first five queue items the learner has not dealt with yet,
so the list refills as the learner works and is empty only when the course is
complete.

When a learner has worked through a full list (or failed a quiz, or was just
enrolled), Moodle writes a refresh request, which is answered for **that
learner only**, in about a second (BKT; the deep model adds model loading).

Since plugin 0.3.0 **Moodle calls the engine**: the request queues an ad hoc
task that asks the engine service (`engine-api.py`, [api/README.md](api/README.md))
to recompute the learner, downloads the bundle and imports it; a scheduled
task rebuilds every course at 03:00. The engine only reads Moodle's database.
Without an engine URL in the plugin settings, the polling worker does the same
job from the engine side (development and fallback):

```bash
.venv/bin/python engine-api.py                          # the service (needs API_TOKEN and MOODLE_DOCKER_DBPASS)
python moodle-sync.py worker --course 10                # fallback: polls every 15 s, full rebuild daily at 03:00
python moodle-sync.py worker --course 10 --once         # one poll (for cron)
python moodle-sync.py refresh --course 10 --learners 7  # by hand
```

A per-learner refresh produces exactly the rows a full run would produce for
that learner (`tests/test_moodle_sync.py`); it never retrains.

## Moodle

Students see their own list in the block `block_microlearning`. Teachers and
managers see, through `local_microlearning` (course navigation > _Learner
profiles_), the cohort (levels, units, hardest topics, every learner) and each
learner's knowledge profile: topics by level, skills against the cohort, a unit
x skill map, the topics of a unit, weak topics and strengths, quiz scores over
time, and what the learner's block shows now. Simulated learners are marked;
a model trained on simulated learners is never applied to a real one.

Installation, the bundle, the import rules, the refresh flow and what has been
verified: [moodle_sync/README.md](moodle_sync/README.md).

## Preparing the Moodle course

The English test course and simulated learners for the local Moodle instance
are fully scripted. Run `python moodle-setup.py build-course`, then follow the
runbook in [moodle_setup/README.md](moodle_setup/README.md). The course has one
short lesson per KC and activity completion on every activity (pages on view,
quizzes on a 70 % pass grade).

To prepare another course by hand, follow two conventions so the collector can
export the course's Knowledge Components and their resources:

- one question category per KC with an **ID number** (the `kc_id`), placed
  under a parent category named after its dimension (grammar, vocabulary, ...);
- optionally, a learning resource per KC whose **activity ID number** is the
  `kc_id`, so recommendations can point to it (`kc_activity_map.csv`).

Details:
[learner_model/docs/mapping_documentation.md](learner_model/docs/mapping_documentation.md).

## Packages

| Package              | Role                                                                  | Docs                                        |
| -------------------- | --------------------------------------------------------------------- | ------------------------------------------- |
| `course_workspace/`  | One folder per course; migration from the old layout                  | module docstring                            |
| `collector/`         | Moodle database -> CSV exports, including the KC catalog and mappings | [README](collector/README.md)               |
| `knowledge_engine/`  | Learner-by-KC knowledge states, phases and promotion gate, simulator  | [README](knowledge_engine/README.md)        |
| `learner_model/`     | Learner profiles, knowledge levels and their components               | [README](learner_model/README.md)           |
| `knowledge_tracing/` | Deep-KT toolkit: benchmark experiment and course-model training       | [README](knowledge_tracing/README.md)       |
| `moodle_setup/`      | Scripted English course and simulated learners in local Moodle        | [README](moodle_setup/README.md)            |
| `recommender/`       | Explainable recommendation queues and their offline replay            | [README](recommender/README.md)             |
| `moodle_sync/`       | Bundle, import, per-learner refresh worker; the two Moodle plugins    | [README](moodle_sync/README.md)             |
| `api/`               | Engine service Moodle calls for refreshes, full runs and bundles      | [README](api/README.md)                     |

The current state and open items are summarised in `thesis_docs/HANDOFF.md`
(a local working note; `thesis_docs/` is not tracked). An earlier topic-level
recommender, its HTTP API and its Moodle plugins were removed; they remain in
git history (Moodle fork commit `2dfbc4dd519`). The current `api/` is the
engine service added with plugin 0.3.0, not that API.
