# Personalized Microlearning Recommendation Using Moodle Learning Analytics

A microlearning course splits a subject into many small activities: short
lessons, practice quizzes and checks, each about one skill. Moodle shows them
in course order, the same for every learner. A learner who struggles with one
skill is not sent back to it, and a learner who has mastered it is not moved
on.

This repository proposes a system that, for every learner of a Moodle course,

1. **models the learner**: estimates what they know per skill and summarises
   it, together with how they work, as a learner profile; and
2. **recommends the next activities**: a ranked list with a reason the
   learner can check against their own results.

Learners see their recommendations and their own profile inside Moodle;
teachers see the profiles of their group. Everything is computed from **the
course's own data**. No external dataset, model or learner record is used to
score a course's learners.

## The problem and the constraints the design answers

| Constraint | Consequence in the design |
| --- | --- |
| A new course has no data, but every learner needs a profile from day one. | Knowledge estimation starts with a model that needs no training (BKT) and lets a trained deep model take over only when the course's own data shows it is better ([§ 3](#3-knowledge-estimation-grows-with-the-course)). |
| Learners and teachers must be able to trust a recommendation. | The recommender is rule-based: every item carries a reason in countable terms ("2 of 8 questions correct") and says whether it rests on the learner's answers, a model estimate, or course order ([§ 5](#5-recommendation)). |
| The course designer, not the engineer, defines the skills. | Skills (Knowledge Components) are read from ordinary Moodle authoring: question categories, competencies, ID numbers ([§ 2](#2-how-a-course-describes-its-skills)). |
| Feedback must arrive while the learner is still working. | A failed quiz changes the list at once, and the learner's profile is recomputed in about a second ([§ 6](#6-delivery-and-real-time-refresh)). |
| Moodle must not depend on an analytics server being up. | The engine only reads Moodle's database; Moodle stores the latest results and keeps working from them. |
| Results must be traceable for a thesis. | Every stage writes a manifest of what it read (hashes, cutoff time, model, mapping version). |

## 1. Architecture

```
Moodle database
   │  collector          read-only export: learners, activities, quiz answers, events, completion
   ▼
data/                    snapshot of the course, including its Knowledge Components (KCs)
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
Moodle: local_microlearning (storage, profile pages, refresh requests) + block_microlearning (learners)
```

The first three stages are **Phase 1** of the project (learning analytics and
the learner model); the recommender and its delivery are **Phase 2**
(personalised recommendation). Each stage reads only the files of the stages
before it, so any stage can be rerun, replaced or evaluated on its own, and a
past state of the course can be replayed by cutting the data at a time
(`--as-of`).

## 2. How a course describes its skills

A **Knowledge Component (KC)** is one skill that a question tests. A course
defines its KCs with Moodle's own authoring tools, which the collector reads:

- each **question category with an ID number** is a KC (`kc_id`); its parent
  category is the KC's **dimension** (for a language course: grammar,
  vocabulary, ...);
- an activity linked to a KC's **competency** (competency ID number =
  `kc_id`) is that KC's learning resource, so a weak KC leads straight to its
  lesson. A course that does not use competencies may give the activity the
  `kc_id` as its course-module ID number instead;
- a **quiz's role** is the prefix of its course-module ID number (`CHECK-`,
  `PRACTICE-`, `CHECKIN-`, `REVIEW-`, `CHALLENGE-`); a quiz without one is a
  check quiz;
- optional **question tags** (`ml-difficulty-N`, `ml-cefr-xx`,
  `ml-check`/`ml-pool`) carry authored difficulty and level.

The system works with any course that follows the first convention. It gives
the most precise feedback for the course design it was built for, **one
activity = one KC**:

| Per unit | Per KC | At the end |
| --- | --- | --- |
| a **check-in** before teaching (one question per KC) and, from the second unit, a **review** of earlier KCs | a **lesson**, a **practice** quiz (random questions, no pass mark) and a **check** quiz on that KC alone | a **challenge** quiz per KC (hard questions) |

In this design a failed check points at exactly one KC, so "revise what?"
has a direct answer. Knowledge estimation works per question in any layout,
because every question has exactly one KC; the layout matters for quiz-level
signals (pass/fail, completion) and for how much evidence reaches the learner
model before a KC is taught. The collector reports the course's granularity
and roles in `kc_mapping_report.json`.
Details: [learner_model/docs/mapping_documentation.md](learner_model/docs/mapping_documentation.md).

## 3. Knowledge estimation grows with the course

1. **Cold start.** Bayesian Knowledge Tracing (BKT) with fixed parameters.
   Every enrolled learner has a profile from the first day; an unanswered KC
   holds a prior, never a score.
2. **Training.** Once enough learners have answered enough questions
   (`knowledge-engine.py status`), a deep knowledge-tracing model (AKT) is
   trained on the course's own answers. Training is a deliberate step, never
   automatic.
3. **Personalised.** The deep model takes over only if it beats BKT on
   held-out learners of the same course (the **promotion gate**). Otherwise
   BKT stays in charge.

A trained model scores only the learner population and the question mapping
it was trained on; everyone else gets BKT.
[knowledge_engine/README.md](knowledge_engine/README.md)

The choice of AKT is justified separately, by a benchmark of five deep models
on public datasets ([knowledge_tracing/README.md](knowledge_tracing/README.md)).
The benchmark never feeds a course: a model is tied to the questions and
skills it was trained on.

## 4. The learner profile

The learner model joins the knowledge states with the activity logs into one
profile per enrolled learner:

- **knowledge**: per-KC estimate and a **knowledge level** (not assessed,
  started, weak, developing, strong) computed from the learner's own answers
  only;
- **skills and dimensions**: the same per unit × dimension and per dimension;
- **weak areas**: KCs of concern, each marked as based on answers or on a
  prediction;
- **behaviour, engagement, history**: sessions and the gaps between them,
  practice and revisits, estimated time, completion and course progress,
  active days and quiz attempts. These describe how a learner works, not how
  well they learn.

[learner_model/README.md](learner_model/README.md)

## 5. Recommendation

The recommender turns a profile into a **queue** of every open activity,
remediation first. Rules decide *what* is worth recommending: revise the
lesson of a failed check, retry it, revisit an earlier prerequisite after
failing again, review or practise a weak KC, prepare for the next check,
challenge a strong KC, continue the course. A fixed slot layout decides *how
many of each* reach the visible top five, so a learner is never stuck in
remediation. Precomputed **failure branches** ("if this quiz is failed")
let Moodle react to a failed quiz without waiting for the engine.

Each item carries a reason code, a sentence for the learner, its **basis**
(`evidence`, `prediction` or course order) and the numbers behind it for the
teacher. [recommender/README.md](recommender/README.md)

## 6. Delivery and real-time refresh

The plugins store the latest snapshot and display it. The learner's block
shows the next five items of their queue that they have not dealt with yet,
so the list refills as they work. When a learner finishes their list, fails a
quiz or enrols, Moodle asks the **engine service** to recompute that learner
only (about a second), downloads the result and imports it after checking its
checksums; a nightly task rebuilds the whole course. If the engine is down,
the block keeps working from the stored queue.
[moodle_sync/README.md](moodle_sync/README.md), [api/README.md](api/README.md)

## 7. Evaluation approach

| Question | How it is answered | Where |
| --- | --- | --- |
| Which knowledge-tracing model to use? | Five deep models against BKT and logistic baselines on public datasets (EdNet-KT1 and a synthetic English log) | `knowledge_tracing/`, notebooks 10–15 |
| Does the deep model help in this course? | The promotion gate on held-out learners of the course; for simulated learners also the oracle AUC (the best any model could reach on that generator) | `knowledge-engine.py train`, `oracle` |
| Do the rules beat simpler recommenders? | Offline replay at past cutoffs against five comparison policies (course order, popularity, random, content-based, item-based collaborative filtering), with Hit@N, MRR, Precision/Recall/NDCG@N, targeting of weak KCs and stability | `recommend.py evaluate` |
| Does the whole chain work inside Moodle? | A scripted test course with simulated learners whose answers are stored by Moodle itself, then the full pipeline, import and refresh | `moodle_setup/` |

**Limits of the evidence.** Nobody has yet followed these recommendations, so
the replay measures agreement with what learners did next and whether weak
KCs were really weak, not learning gain or completion. Results on simulated
learners show that the system works end to end; they describe the simulator,
not real learners. Thresholds, slot sizes and the prerequisite relation are
design choices, not calibrated values.

## Repository

### Packages

| Package              | Role                                                                  | Docs                                  |
| -------------------- | --------------------------------------------------------------------- | ------------------------------------- |
| `collector/`         | Moodle database → CSV snapshot, including the KC catalog and mappings | [README](collector/README.md)         |
| `knowledge_engine/`  | Learner × KC knowledge states, phases and promotion gate, simulator   | [README](knowledge_engine/README.md)  |
| `learner_model/`     | Learner profiles, knowledge levels and their components               | [README](learner_model/README.md)     |
| `recommender/`       | Explainable recommendation queues and their offline replay            | [README](recommender/README.md)       |
| `moodle_sync/`       | Bundle, import, per-learner refresh; the two Moodle plugins           | [README](moodle_sync/README.md)       |
| `api/`               | Engine service Moodle calls for refreshes, full runs and bundles      | [README](api/README.md)               |
| `course_workspace/`  | One folder per course                                                 | module docstring                      |
| `knowledge_tracing/` | Deep-KT toolkit: model benchmark and course-model training            | [README](knowledge_tracing/README.md) |
| `moodle_setup/`      | Evaluation test bed: scripted course and simulated learners           | [README](moodle_setup/README.md)      |

The Moodle plugins live in the `moodle` submodule (a Moodle 5.2 fork) under
`public/local/microlearning` and `public/blocks/microlearning`; the local
Docker setup is the `moodle-docker` submodule.

### One workspace per course

Everything the course pipeline reads and writes lives in the course's
workspace, so one engine can serve several courses and Moodle sites:

```
courses/<site>/<course_id>/        site: $MICROLEARNING_SITE or "local"; root: $MICROLEARNING_HOME or ./courses
    course.json  data/  knowledge/  kt/  learner_model/  recommendations/  evaluation/  bundle/  refresh/  worker/
```

`python -m course_workspace show --course <id>` prints a course's folders;
`list` shows all workspaces.

### Running it

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
package of the same name (`python -m <package>` is equivalent). The pipeline
stage scripts accept `--help` and `--course <id>`; `moodle-setup.py` selects
its course by `--shortname`, and `engine-api.py` takes only `--host`/`--port`.

| Script                                                                                            | Does                                                    |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `moodle-collector.py [--learners ID,...]`                                                         | export the course from Moodle's database                |
| `knowledge-engine.py status\|estimate\|train\|simulate\|oracle`                                   | knowledge states, training and promotion gate           |
| `learner-model.py`                                                                                | learner profiles                                        |
| `recommend.py generate\|evaluate`                                                                 | recommendation queues; offline replay against baselines |
| `moodle-sync.py bundle\|push\|refresh\|worker\|stages`                                            | bundle, import, per-learner refresh                     |
| `engine-api.py`                                                                                   | the engine service Moodle calls                         |
| `moodle-setup.py build-course\|simulate-learners\|annotate-plan\|remove-simulated\|check-content` | test course and simulated learners                      |
| `./run_pipeline.sh --course ID [--no-push]`                                                       | collector to Moodle in one command                      |
| `python -m course_workspace show\|list\|migrate`                                                  | course workspaces                                       |

### Testing without real learners

`moodle-setup.py` builds a complete test course in a local Moodle and injects
simulated learners through Moodle's own APIs, so every stage runs on data that
went through Moodle ([moodle_setup/README.md](moodle_setup/README.md)). The
knowledge engine can also simulate answers offline, into a separate
workspace:

```bash
python knowledge-engine.py simulate --output-dir data/simulated
python knowledge-engine.py train    --site offline --course <id> --data-dir data/simulated
python knowledge-engine.py estimate --site offline --course <id> --data-dir data/simulated
```

Simulated learners are labelled per learner (`is_simulated`) and throughout
(`source_population`); a model trained on them never scores a real learner.
