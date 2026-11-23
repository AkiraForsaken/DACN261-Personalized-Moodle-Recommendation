# moodle_setup: English course and simulated learners in local Moodle

Builds the **English Microlearning** course in the local `moodle-docker`
instance, in one of two layouts (`build-course --layout`):

| Layout              | Course         | Quizzes                                                            | Activity -> KC                        |
| ------------------- | -------------- | ------------------------------------------------------------------ | ------------------------------------- |
| `kc` (default)      | `ENG-MICRO-KC` | per unit a **check-in**; per KC a **practice** and a **check** quiz; a **review** per unit from unit 2; a **challenge** quiz per KC (303 quizzes) | course-module **competency** (kc_id); quiz role = course-module ID number |
| `dimension`         | `ENG-MICRO`    | one practice quiz per unit and dimension, ~3 KCs each (32 quizzes) | course-module ID number = kc_id       |

`kc` is the microlearning layout, modelled on EdNet/Santa (diagnose, practise
with feedback, check, review): every activity except the check-in and the
review concerns one KC, so a failed check points at one KC. It is built as
course 12 (rebuilt 2026-11-22; the earlier one-check-per-KC version was
course 11, archived in `courses/local/11/`). `dimension` is the original
course (id 10), kept as the mixed-quiz baseline. Both use the same check
questions, KC question categories and lessons; only `kc` uses the practice
pool, the question tags and the quiz roles. It is built and fills it with simulated learners whose quiz
attempts, page views and activity completion are stored in Moodle itself. The
course pipeline (`collector` -> `knowledge_engine` -> `learner_model`) can then
be tested on data that went through the whole chain, Moodle included.

Everything is scripted. Python prepares the content and the learner plan.
PHP scripts run inside the `webserver` container and use Moodle's own APIs. The
Moodle checkout is not modified: scripts and JSON inputs are copied to
`/tmp/moodle_setup` in the container with `docker cp`.

## What gets built

| Moodle object | Contents                                                                                                                                                                                         |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Course        | `ENG-MICRO-KC` (layout `kc`) or `English Microlearning` (`ENG-MICRO`, layout `dimension`), topics format, completion tracking on, start date 150 days ago                                        |
| Sections      | `Unit 1: Everyday Routines` ... `Unit 8`, one per unit; layout `kc` adds a 9th section, `Challenge practice`                                                                                     |
| Question bank | One course `qbank` with categories `top > Grammar / Vocabulary / Reading / Writing > one per KC`. Each KC category has **ID number = `kc_id`**, name = KC name, description = learning objective |
| Questions     | 390 single-answer multiple-choice check questions (4 or 5 per KC), options shuffled. Layout `kc` adds the 384-question practice pool (4 per KC) and tags every question: `ml-check`/`ml-pool`, `ml-difficulty-N`, `ml-cefr-xx` |
| KC lessons    | One page per KC, `Lesson: <KC name>`: explanation, examples, common mistake, self-check items with hidden answers, optional video. Layout `dimension`: **course-module ID number = `kc_id`**; layout `kc`: no ID number (the competency names the KC) |
| Per unit      | Layout `kc`: overview page, `Unit N check-in` (`CHECKIN-UN`: one random pool question per KC of the unit, 1 attempt), then per KC its lesson, `Practice: <KC>` (`PRACTICE-<kc>`: 2 random pool questions) and `Check: <KC>` (`CHECK-<kc>`: its 4-5 fixed check questions, pass 7/10), and from unit 2 `Unit N review` (`REVIEW-UN`: one random pool question on each of 8 earlier KCs). Section 9: `Challenge: <KC>` (`CHALLENGE-<kc>`: 2 random difficulty-3 pool questions). Layout `dimension`: overview page, then per dimension: that dimension's KC lessons followed by its practice quiz (`Unit N Grammar practice`, ...). All quizzes: immediate feedback, unlimited attempts except the check-in |

That is 96 KCs, 96 lessons and 8 overview pages per course, with 303 quizzes
(layout `kc`: 8 check-ins, 96 practice, 96 check, 7 review, 96 challenge; 536
random slots) or 32 quizzes (layout `dimension`).

**Competencies (layout `kc`).** The site-wide framework `ENG-MICRO-KCS` holds
one competency per KC (ID number = `kc_id`, name = KC name, description =
learning objective) under one parent competency per dimension (`DIM-Grammar`,
...). The framework is reused on a rebuild (competencies are updated, missing
ones created). Every KC competency is added to the course, and every lesson
and quiz is linked to the competencies of its KCs (536 links: lessons,
practice, check and challenge quizzes one each, a check-in its 12 KCs, a
review its 8; rule outcome "none": no automatic ratings). In this layout
lessons carry **no** ID number, so the competencies really are the source.
`build-course` stops if competencies or tags are switched off for the site
(_Site administration > Advanced features_); it never changes those settings.
Questions stay in their KC category: Moodle has no per-question competency.

**Pass mark of a check quiz.** 7/10 as everywhere; with 4 questions that means
3 of 4 right, with 5 questions 4 of 5 (80 %).

If the plugin `block_microlearning` is installed, `build-course` also adds the
"Recommended for you" block to the course (see
[moodle_sync/README.md](../moodle_sync/README.md)). Publishing profiles and
recommendations to Moodle is `python -m moodle_sync push`.

**Activity completion** (all set by the script):

| Activity                | Complete when                                                                                   |
| ----------------------- | ----------------------------------------------------------------------------------------------- |
| Overview and KC lessons | the learner views the page                                                                      |
| Check quiz (`kc`), practice quiz (`dimension`) | the learner reaches the **pass mark, 7/10 (70 %)**. Below it Moodle records `complete_fail` (3) |
| Check-in, practice, review, challenge (`kc`)   | the attempt is graded (no pass mark): `complete` (1)                       |

The collector derives everything else from this layout at run time, with no
hand-maintained mapping file (see `collector/collect_course_mappings.py`):

- `kc_catalog.csv` and `moodle_question_kc_map.csv` from the question
  categories;
- `kc_topic_map.csv` from the quizzes that use each KC's questions;
- `kc_activity_map.csv`: `role=learn` for the activity linked to the KC's
  competency (layout `kc`) or whose ID number is the `kc_id` (layout
  `dimension`), `role=practice` for the quizzes holding or drawing the KC's
  questions (fixed and random slots);
- `activity_catalog.csv` `quiz_role` from each quiz's ID number;
- `question_metadata.csv`: difficulty, item set and CEFR level from the tags;
- `kc_mapping_report.json`: which source was used, how many quizzes test one
  KC, quizzes per role, pool difficulty coverage, and any quiz whose
  competencies disagree with its questions.

A recommender can therefore go from a weak KC or a failed quiz straight to the
lesson to revise.

## Course content

Edit the content in these files, not in Moodle (layout `kc` additionally
requires at most 5 check questions per KC and a complete practice pool,
`check-content --layout kc`):

```
course/kc_catalog.csv             kc_id, unit, unit_name, dimension, kc_name, learning_objective, cefr_level
course/questions/unitN.txt        check questions: kc_id | difficulty | stem | correct option | wrong | wrong | wrong
course/questions/pool/unitN.txt   practice pool (layout kc only), same format
course/lessons/unitN.md           one lesson per KC (format below)
```

`difficulty` is 1 (easy), 2 or 3 (hard), and `cefr_level` is A1-B2. Both are
authored labels drafted with AI assistance (2026-11-22), not calibrated on
learner data; review them before relying on them. Layout `dimension`
(course 10) ignores difficulty, CEFR level and the pool, so its build spec is
unchanged (pinned by a digest in `tests/test_moodle_setup.py`).

Layout `kc` uses the pool for random questions only: a practice quiz per KC
(2 questions), a check-in per unit (one per KC of the unit, before teaching),
a review quiz from unit 2 (two earlier KCs per dimension) and a challenge
quiz per KC (2 difficulty-3 questions). Each pool needs at least 4 questions
per KC, every difficulty, and at least 2 of difficulty 3; a pool question
never repeats a check question, so practice never shows a check answer. Each
quiz's role is its course-module ID number (`CHECK-KC001`, `PRACTICE-KC001`,
`CHECKIN-U3`, `REVIEW-U3`, `CHALLENGE-KC001`); check and pool questions share
the KC's question category and are told apart by the tags `ml-check` /
`ml-pool`, next to `ml-difficulty-N` and `ml-cefr-xx`.

The correct option is always written first, and Moodle shuffles the options in
each attempt. Reading questions carry their short passage in the stem. Writing
questions are of the "choose the correctly written sentence / best revision"
kind, because free writing cannot be auto-graded for knowledge tracing.

Lesson format (`**bold**` and `~~wrong form~~` are supported):

```
## KC001
video: https://...            optional; empty until a video exists
### Explanation
One or more short paragraphs.
### Examples
- one example per bullet
### Common mistake
- one mistake per bullet
### Try it
- A self-check question => its answer (hidden until the learner opens it)
```

To turn a lesson into a video lesson later, put the video's URL in `video:`.
A direct `.mp4` / `.webm` link is embedded in the page; any other URL is shown
as a link. Then rebuild the course.

> **Review needed.** The questions and the lessons were drafted by an AI
> assistant. Check them (correct key, one defensible answer, level, accuracy of
> the explanations) before using any result in the thesis.

`python -m moodle_setup check-content` validates the content without touching
Moodle. It checks that every KC has at least 4 questions with 4 distinct
options, that no question is duplicated, that every KC has exactly one
complete lesson with at least 2 self-check items, and that no self-check item
repeats a quiz question (a lesson must not show quiz answers). `build-course`
runs the same checks first.

## Scripted vs. Moodle UI only

Everything this course needs is created by the scripts, including activity
completion, pass marks and the lesson ID numbers. What the scripts deliberately
do not do:

| Setting                                                        | Why not scripted                                                                                                                                   | Where to change it                                                        |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Site-wide **Enable completion tracking**                       | A site setting affecting every course. `build-course` checks it and stops with a message if it is off; it never changes it.                        | _Site administration > Advanced features_                                 |
| **Course completion** criteria (when the whole course is done) | Scriptable, but the learner model does not use it, so it is left unset.                                                                            | _Course > Course completion_                                              |
| Uploaded **video files**                                       | Videos do not exist yet. A URL in a lesson's `video:` field is enough; uploading files into Moodle would need the file API and is not implemented. | Lesson `video:` field (URL), or edit the page in Moodle (lost on rebuild) |

Anything changed by hand in the Moodle UI is lost on `build-course --rebuild`.
Put lasting changes into `course/` and the build script instead.

## Prerequisites

- The `moodle-docker` containers are running. The scripts expect the web
  container to be `moodle-docker-webserver-1`; set `MOODLE_WEBSERVER_CONTAINER`
  if yours is named differently (`docker ps`).
- The Python dependencies are installed: `pip install -r requirements.txt`
  (the collector needs its `mariadb` package), plus
  `pip install -r requirements-kt.txt` for `knowledge_engine train`.
- Your usual `MOODLE_*` collector settings are exported (see `collector/config.py`).
- **Back up the Moodle database first.** Injection writes to it. The database
  container runs MariaDB 11.4:

  ```bash
  # Uses the credentials Moodle itself connects with (the db container's own
  # MYSQL_PASSWORD may not match them).
  P=$(docker exec moodle-docker-webserver-1 printenv MOODLE_DOCKER_DBPASS)
  docker exec -e P="$P" moodle-docker-db-1 sh -c 'mariadb-dump -u moodle -p"$P" moodle' > moodle_backup.sql
  # restore: docker exec -i -e P="$P" moodle-docker-db-1 sh -c 'mariadb -u moodle -p"$P" moodle' < moodle_backup.sql
  ```

## Runbook

The commands below use the `python -m <package>` form. The scripts in the
repository root are equivalent: `moodle-setup.py`, `knowledge-engine.py`,
`learner-model.py` (see the root README).

```bash
# 0. Build the course once. It prints the course id. Default layout kc
#    (ENG-MICRO-KC); --layout dimension builds the old ENG-MICRO.
python -m moodle_setup build-course
export MOODLE_COURSE_ID=<id>       # BEFORE any --plan-out below: with it unset,
                                   # courses/local/$MOODLE_COURSE_ID/... becomes
                                   # courses/local//data/, outside every workspace
# simulate-learners / annotate-plan work on ENG-MICRO-KC unless given
# --shortname ENG-MICRO (course 10's plans).

# 1. Phase 1 on an empty course: every enrolled learner gets a cold-start profile.
python moodle-collector.py
python -m knowledge_engine estimate
python -m learner_model.builder

# 2. Phase 1 with a few learners: not enough data for a deep model -> BKT.
python -m moodle_setup simulate-learners --learners 20 --seed 7 \
    --plan-out courses/local/$MOODLE_COURSE_ID/data/plan_20.json   # a literal path: keep it in the workspace
python moodle-collector.py
python -m knowledge_engine status                 # expect: not ready
python -m knowledge_engine estimate
python -m learner_model.builder

# 3. Phases 2-3: the full cohort. Same seed: learners 1-20 are kept, 21-300 added.
python -m moodle_setup simulate-learners --learners 300 --seed 7 \
    --plan-out courses/local/$MOODLE_COURSE_ID/data/plan_300.json
python moodle-collector.py
python -m knowledge_engine status                 # expect: ready
python -m knowledge_engine train                  # tuned epochs, seeds 42 43 44, then the gate
python -m knowledge_engine estimate
python -m learner_model.builder

# 4. Clean up when done.
python -m moodle_setup remove-simulated
python moodle-collector.py                        # export without the simulated learners
```

Injecting 20 learners takes under a minute; 300 learners (course 10: about
2,300 attempts, 28,000 answers, 2,000 page views) take several minutes.

**Data volume in layout `kc` (measured, course 12, 300 learners, seed 7,
2026-11-22).** 4,296 attempts (1,890 check, 1,888 practice, 308 check-in, 197
challenge, 13 review) and 15,748 answers; all 96 KCs have at least 20 answers,
so `status` is ready. Review quizzes are rare because a simulated learner
seldom finishes all 12 KCs of a unit. `--session-scale` (default 3.5; mean
sessions about 1 + 2 x scale) can raise the volume, but that changes the
generator and must be reported as such. The same `SIM-` accounts are enrolled in each
course they are simulated into; `remove-simulated` deletes them site-wide,
from both courses. Progress is printed
every 10 learners. If the run is interrupted, run the same command again:
learners whose attempts already exist are skipped, and an unfinished attempt is
deleted and redone.

**Keep the same `--seed`** when you extend a cohort. A plan for 300 learners
starts with exactly the 20 learners of the plan for 20 learners with the same
seed, which is what makes resuming and extending safe. With a different seed,
the existing learners' remaining attempts would come from a different learner.

`simulate-learners --dry-run --plan-out plan.json` creates the plan and
reports its size, page behaviour and generator AUC without writing anything to
Moodle.

`annotate-plan --plan plan.json` regenerates a saved plan from its seed and the
course's structure, refuses unless it is identical, and writes
`plan.annotated.json` with each answer's generating probability (`p_correct`)
and each learner's Moodle user (`SIM-<number>`; read-only in Moodle). The
oracle AUC needs it (`knowledge-engine.py oracle`, see
[knowledge_engine/README.md](../knowledge_engine/README.md)).

### After changing the course

Changing the content (questions, lessons, catalog) means rebuilding the
course, which deletes every attempt in it:

```bash
python -m moodle_setup remove-simulated
python -m moodle_setup build-course --rebuild     # new course id: export MOODLE_COURSE_ID again
                                                  # (add --layout dimension to rebuild ENG-MICRO)
# then steps 1-3 of the runbook, including a retrain
```

The rebuild creates new question-bank entries, so the question-to-KC
`mapping_version` changes. A previously promoted deep model is then no longer
used: `estimate` falls back to BKT with the reason "mapping changed since
training; retrain required" until `train` is run on the new data. Moodle's
recycle bin keeps a backup of the deleted course (_Course category > Recycle
bin_) until it expires. Real test students must be enrolled again in the new
course. The plugin tables keep the deleted course's snapshot and rows (nothing
removes them on course deletion), so the nightly full sync tries that course
every night and logs a failure until they are removed.

## How the simulated learners are made

**Python decides, PHP replays.** `export_structure.php` reports the course's
quizzes (slots, question-bank entries, KCs, pass mark), the KC lessons (pages
linked to a KC competency, or, without module competencies, pages whose ID
number is a `kc_id`) and the unit overview pages.
`knowledge_engine.simulator.simulate_plan` decides every learner's sessions.
It uses the same answer generator as offline simulation: an item-response model
with practice gains and forgetting, deliberately not BKT. About 5 % of
learners enrol but never answer, so they are cold-start learners.

One session is one attempt at one real quiz, answering all its questions. Around
the attempts, each learner:

- views the unit's **overview** before their first attempt in a unit;
- with probability `study_propensity` (a learner trait) reads the **lessons**
  of some of a quiz's KCs before their first attempt at it;
- after **failing** a quiz, with probability `revise_propensity` makes the next
  session a **revision**: reads the lessons of the KCs they answered below the
  pass mark in that attempt, then retries the same quiz;
- sometimes **reviews** a finished attempt a few minutes later;
- moves on to the next quiz after passing, and sometimes goes back to an
  earlier one.

**Courses with quiz roles (layout kc)** use `simulate_unit_plan` instead,
with the same answer model. Item and KC difficulty follow the authored labels
(`DIFFICULTY_OFFSET`, `CEFR_OFFSET`, plus noise), so "hard items are answered
less often" is built into the generator. Navigation follows the unit
sequence: overview and check-in on entering a unit; per KC lesson (by
`study_propensity`), 0-2 practice attempts, then the check; after a fail a
later retry (revising first by `revise_propensity`) and a move on after 3
failed checks in a row; a unit's review quiz after its last KC (p 0.5); a
session of practice on an earlier checked KC (p 0.15); a challenge attempt
after a perfect check (p 0.2). Random slots draw distinct candidates per
attempt and the plan records them per slot (`slots`). All values are generator
assumptions, recorded under `navigation_assumptions` in the plan manifest.
Courses without roles keep the original simulator exactly (pinned by digests
in `tests/test_moodle_setup.py`).

**Assumption:** reading a lesson adds `lesson_gain x learn_rate` to that KC's
practice gain (`--lesson-gain`, default 0.5, i.e. half the gain of the first
practice answer; `0` = lessons have no effect). This is a property of the
generator, chosen so recommendation logic has something to act on. It is not
evidence that the lessons help, and results that depend on it must say so.
The value is recorded in the plan manifest under `page_behaviour`.

`inject_attempts.php` replays the plan through Moodle's APIs:

- It creates users `sim0001`, `sim0002`, ... with ID number `SIM-0001`, ...
  (auth `nologin`, so they cannot sign in) and enrols them as students at the
  planned date.
- Page views go through `page_view()`, which logs the view and sets the page's
  "viewed" completion.
- Each attempt goes through `quiz_view()` and the normal quiz flow: create,
  start, answer each question at its simulated time (the planned right or
  wrong option, resolved through that attempt's shuffled order), submit,
  grade. Grading updates the quiz's pass-grade completion.
- Reviews fire the attempt's `attempt_reviewed` event.
- A random slot gets the question the plan chose for it
  (`setup_start_attempt_with_questions` in `lib.php`). Core
  `quiz_start_new_attempt()` can force only one random slot in 5.2 (it looks
  forced ids up by `next_slot_number()`, which stays 1 while it fills random
  slots), so the helper follows core's steps itself and checks every forced
  question with core's own slot filter.
- The script **stops** if Moodle's grade differs from the plan, or if a quiz's
  completion state is not `complete_pass` / `complete_fail` as the learner's
  best grade says (or `complete` for a quiz without a pass mark), or if a
  planned question does not match its random slot's filter.
- Moodle stamps everything with the current time. After each step, the rows the
  step created are moved to the simulated time: log rows, activity completion
  (`course_modules_completion`), module views (`course_modules_viewed`) and the
  first step of each question attempt. These are direct database updates,
  acceptable on a test instance but not something Moodle supports officially.

## Labelling

The collector labels **each learner**: `learner_roster.csv` has an
`is_simulated` column, 1 for an account with a `SIM-` ID number and 0 for any
other. The knowledge engine and the learner model carry that label on every
row as `source_population` (`simulated` or `moodle_collector`).

A real test student enrolled next to simulated learners therefore stays a real
learner: it is not labelled `simulated`, and it is not scored by a deep model
that was trained on simulated learners (it keeps the Phase 1 BKT estimate until
real data passes the gate itself). Such a cohort is reported as `mixed` in the
manifests, with the number of learners of each kind.

When any exported learner is simulated, the collector also writes
`simulation_manifest.json` into its output directory, with `cohort` set to
`simulated` or `mixed`.

After a successful injection, `simulate-learners` also writes
`simulation_plan_manifest.json` (seed, cohort size, accuracy, generator AUC
over all answers (`ceiling_auc`), page behaviour, all generator settings) into
the course's `data/` folder. That AUC covers every generated answer, so it is
**not** comparable with the gate's AUCs on held-out targets; use
`knowledge-engine.py oracle` for the same-target value. When the collector
finds no simulated learners, it removes both manifests.

## Checks after each step

1. **After `build-course`**: the summary prints `Quizzes by role: {"challenge": 96,
   "check": 96, "checkin": 8, "practice": 96, "review": 7}; 536 random slots`
   (layout `kc`). In the browser: 8 unit sections (overview, check-in, then
   lesson / practice / check per KC, review from unit 2) and `Challenge
   practice`. Every activity shows its completion condition. The question bank
   has 4 dimension categories and 96 KC categories with ID numbers; questions
   carry their `ml-` tags. Preview one practice quiz (two different pool
   questions) and one lesson. Layout `dimension`: lessons and a quiz per
   dimension.
2. **Collector** (layout `kc`): `kc_catalog.csv` has 96 rows,
   `moodle_question_kc_map.csv` and `question_metadata.csv` 774 (390 check +
   384 pool, all labelled); `kc_mapping_report.json` shows `quizzes_by_role`
   as above, `check_quizzes_single_kc` 96 of 96, and empty
   `kcs_without_practice`, `kcs_missing_hard_pool_items`, `cefr_disagreements`
   (all verified on course 12). Enrolled users' profiles start `cold_start_only`.
3. **After 20 learners** (seed 7: 164 attempts, 2,019 answers, 120 lesson
   views, 35 overview views, 31 reviews):
   - `question_answer_events.csv` matches the plan answer for answer;
   - `course_modules_completion.csv` has `complete` rows for viewed pages and
     `complete_pass` / `complete_fail` for quizzes, all with backdated times;
   - `learning_events.csv` holds `content_view` (lessons, overviews, quiz
     views), `quiz_attempt` and `quiz_review` events;
   - `status` says not ready, engagement is `available`, and outputs are
     labelled `simulated`.
4. **After 300 learners**: `status` says ready. `train` completes with three
   seeds and records the gate decision in
   the course workspace's `knowledge/active_model.json`. `estimate` and the builder
   produce Phase 3 or Phase 2 profiles, depending on the gate.
5. **After `remove-simulated`**: the course and the real users are intact.

## Answering quizzes by hand

To add real (non-simulated) answers, enrol an ordinary test student in
`ENG-MICRO-KC` (course 12) or `ENG-MICRO` (_Participants -> Enrol users_), log
in as that student, read some lessons and take a few quizzes. Real accounts have no `SIM-` ID
number, so their rows are labelled `moodle_collector` whether or not simulated
learners are present (see Labelling above).

## Commands

| Command                                                                                                   | Effect                                                                                                                                               |
| --------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `build-course [--layout kc\|dimension] [--rebuild]`                                                        | Creates `ENG-MICRO-KC` (default) or `ENG-MICRO`. Refuses if it exists. `--rebuild` deletes it first, **including all attempts in it**, and creates it under a **new course id**. Run `simulate-learners` again afterwards. |
| `simulate-learners [--learners N] [--seed S] [--lesson-gain G] [--plan-out F] [--dry-run] [--data-dir D]` | Plans and injects N simulated learners in total. Existing ones are kept.                                                                             |
| `remove-simulated`                                                                                        | Deletes every `SIM-` user, with their attempts, log rows and completion records. The course stays.                                                   |
| `check-content`                                                                                           | Validates the course content only.                                                                                                                   |

## Limits

- Results on simulated learners show that the pipeline and the gate work. They
  describe the generator, not real learners, and are not validity evidence.
  This includes any effect of the lessons, which is built into the generator.
- The PHP scripts were written against the Moodle 5.x source in this
  repository. A different Moodle version may need changes to the quiz and
  completion API calls in `php/inject_attempts.php`.
