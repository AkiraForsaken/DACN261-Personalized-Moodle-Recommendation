# moodle_sync and the Moodle plugins

Delivers learner profiles and recommendations to Moodle, keeps them fresh per
learner, and shows them there. Moodle computes no analytics: it stores and
displays what the pipeline produced. Two small parts run live in Moodle,
because they must react instantly: the block works through the learner's
stored queue, and an observer notices when a learner needs a refresh.

```
learner_model + recommender (course workspace)
        │  moodle-sync.py bundle | push          flat CSVs + manifest.json (bundle schema 3)
        ▼
courses/<site>/<id>/bundle/   or  refresh/<stamp>/bundle/ (some learners)
        │  Moodle downloads it from the engine service (api/)
        │  or, without an engine URL: docker cp + plugin CLI (push / worker)
        ▼
local_microlearning   tables, import, refresh requests (observer), teacher pages
        ▼
block_microlearning   "Recommended for you" on the course page (students)
```

**Why a bundle.** The engine and Moodle can run on different machines and at
different times. A bundle is one self-describing package: its manifest names
the course, the kind of update, the time the data was read, and a SHA-256 per
file. Moodle imports it in one transaction or rejects it as a whole, so a
half-written or stale result never reaches learners.

## Install / upgrade

The plugins live in the Moodle fork: `moodle/public/local/microlearning` and
`moodle/public/blocks/microlearning`.

```bash
docker exec moodle-docker-webserver-1 php /var/www/html/admin/cli/upgrade.php --non-interactive
python moodle-sync.py push --course <id>         # first snapshot of a course
```

Current versions: `local_microlearning` 0.4.0, `block_microlearning` 0.2.0
(each plugin's `version.php`).

`build-course` adds the block to a new course when the plugin is installed. On
an existing course, add it by hand: turn editing on, _Add a block_,
_Recommended for you_. Settings: _Site administration > Plugins > Local
plugins > Microlearning profiles_ (refresh requests on/off, refresh after a
failed quiz, and the engine service: `engineurl`, `enginetoken`,
`enginetimeout`).

## Publish and refresh

```bash
./run_pipeline.sh --course <id>                          # full run: collector -> ... -> new snapshot
python moodle-sync.py push --course <id>                 # only the last step
python moodle-sync.py worker --course <id>               # fallback: answer refresh requests (Ctrl-C to stop)
python moodle-sync.py worker --course <id> --once        # one poll, then exit (for cron)
python moodle-sync.py refresh --course <id> --learners 7,12   # refresh named learners by hand
```

### The per-learner refresh loop

1. The block shows the first `top_n` items of the learner's queue that the
   learner has not dealt with since **their** last refresh (the moment the
   pipeline read their data, `profile.timerefreshed`):

   | Reason code                                                     | Dealt with when                                   | Source         |
   | --------------------------------------------------------------- | ------------------------------------------------- | -------------- |
   | continue, start, prepare                                        | the activity's completion is no longer incomplete | completion API |
   | `retry_quiz`                                                    | the quiz is passed                                | completion API |
   | `review_weak_kc`, `revise_before_retry`, `revisit_prerequisite` | the lesson was viewed since                       | standard log   |
   | `practice_weak_kc`, `challenge_strong_kc`                       | a quiz attempt was finished since                 | quiz attempts  |

   "Since" is the learner's refresh, or for a row of an activated failure
   branch the failing attempt. A quiz failed since the refresh (completion
   state "failed") activates its branch: the quiz's lessons, then the retry,
   ahead of everything else (see recommender/README.md, "Failure branches").

   The slot rules (at most 3 remediation items, one forward item kept, prepare
   lessons never take the last slot) are the recommender's: PHP
   `recommendation_window::select` and Python `recommender.ranking.window` are
   both tested against `tests/fixtures/recommendation_window.json`. If nothing
   is left, the block shows the next unfinished activity of the course; it is
   empty only when the course is complete.

2. The observer (`local_microlearning\observer`) writes a row into
   `local_microlearning_refresh` when a learner has dealt with `top_n` items
   (`list_done`), submitted a quiz attempt below its pass mark (`quiz_failed`,
   setting), or was enrolled without a profile (`new_learner`). One pending
   request per learner and course.
3. With an engine URL set, the observer also queues the ad hoc task
   `refresh_learner`, which asks the engine service to refresh the learner,
   downloads the bundle and imports it ([api/README.md](../api/README.md)).
   Without one, the worker polls that table (read-only). Either way the engine
   runs the collector for the requested learners only (`--learners`), the
   model stages in `.venv-kt` (`moodle-sync.py stages`) and builds a
   `learner_update` bundle. The import replaces those learners' rows in the
   active snapshot, sets their `timerefreshed`, and marks their requests
   processed.
4. Once a day the whole course is rebuilt (new snapshot): by the scheduled
   task `full_sync` (03:00) with the engine service, or by the worker
   (`--full-at`, default 03:00). A full run also follows when Moodle refuses
   a learner update because the question mapping changed or no snapshot
   exists.

A refresh reuses the course's estimator decision and trained model; it never
trains. It produces the same rows for its learners as a full run
(`tests/test_moodle_sync.py`). Failures leave the requests pending; the worker
retries with a growing pause (up to 15 min) and records them in
`worker/state.json`.

## The bundle (schema 3)

| File                  | Grain                                                                                                                      | Source                                                                                                                                                          |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profiles.csv`        | learner                                                                                                                    | `learner_profile`, `learner_engagement`, `learner_history`                                                                                                      |
| `kc_states.csv`       | learner x KC                                                                                                               | `learner_kc_state`, `weak_areas` (concern, knowledge level), KC's unit, lesson and practice quiz                                                                |
| `skills.csv`          | learner x skill (dimension x unit)                                                                                         | `learner_skill_summary` + level counts                                                                                                                          |
| `dimensions.csv`      | learner x dimension                                                                                                        | `learner_dimension_summary` + level counts                                                                                                                      |
| `attempts.csv`        | learner x finished quiz attempt                                                                                            | `learner_quiz_attempts`                                                                                                                                         |
| `recommendations.csv` | learner x rank: the full queue, with `group`, then the failure branches (`group = contingency`, `trigger_cmid` = the quiz) | `recommendations.csv`                                                                                                                                           |
| `manifest.json`       |                                                                                                                            | schema 3 (2 + failure branches), `kind` (`snapshot` / `learner_update`), course id, `collected_at`, `top_n`, `max_remediation`, provenance, row counts, SHA-256 |

`bundle` refuses recommendations generated from another learner-model build,
and learner ids that are not Moodle user ids (offline-simulator data).

## Import rules (`local_microlearning`)

A bundle is rejected as a whole if its course id, format version, kind, row
counts or checksums do not match, or if it names users or course modules that
are not in the course (a bundle made before a course rebuild).

- `snapshot`: becomes the course's active snapshot in one transaction; the
  previous snapshot is kept, older ones are deleted.
- `learner_update`: needs an active snapshot with the same question mapping.
  A learner whose stored data is newer than the update is skipped. Only the
  named learners' rows change.

## What users see

- **Learners, on the course page** (`block_microlearning`): the first item of
  their window as an "Up next" card with its kind (Learn, Check; revision
  items marked) and a Start button, the others under "Also recommended".
  Each item shows its reason and a label for its basis ("Based on your
  answers", "Estimate", "Next in the course"), and the block says up to when
  the learner's work was read.
- **Learners, their own profile** (capability
  `local/microlearning:viewownprofile`, course navigation > _My learning
  profile_): knowledge levels, skills, progress per unit, weak and strong
  topics, quiz scores and next steps, in plain language. It leaves out the
  cohort comparison, the evidence numbers and the technical notes.
- **Teachers and managers** (capability `local/microlearning:viewprofiles`,
  course navigation > _Learner profiles_):
  - the group page (`index.php`), filtered by population and by Moodle group
    (only groups the teacher may see): summary tiles, levels across all
    learner × topic pairs, levels per unit, skill means per dimension, a unit
    × skill map, the hardest topics, and a paginated, searchable, sortable
    learner list;
  - a learner's profile (`profile.php?id=<course>&userid=<user>`): the
    learner's view plus the comparison with learners of the same population,
    the evidence behind each level and estimate, and the unit × skill map.

Knowledge levels come from answers only (`learner_model/knowledge_levels.py`).
Chart colours were checked for colour-vision deficiency and contrast, and
every level is also printed as text (`classes/output/knowledge_display.php`).

## Privacy

`local_microlearning` stores per-learner analytics and refresh requests and
implements the Privacy API (export and deletion per user and per course
context). The block stores nothing.

## Deployment (engine on its own server)

Moodle calls the engine ([api/README.md](../api/README.md)): the engine
service (`engine-api.py`) answers refresh and full-run requests with a
finished bundle, which the plugin downloads and imports itself
(`engine_client`, ad hoc task `refresh_learner`, nightly scheduled task
`full_sync`). The engine needs only read access to Moodle's database (for the
collector) and never writes to Moodle. `push`, `refresh` and the polling
`worker` remain for development and as a fallback when no engine URL is set
(`local_microlearning | engineurl`).

## Tests and verification

`tests/test_moodle_sync.py` and `tests/test_worker.py` cover the bundle,
workspace isolation, the worker, and the equivalence of a per-learner refresh
with a full run. The PHP window rule is checked against the shared fixture
inside the web container (`tests/test_recommender.py` skips it when the
container is not running).

```bash
# PHPUnit, once Composer is available in the container:
docker exec -w /var/www/html moodle-docker-webserver-1 composer install
docker exec -w /var/www/html moodle-docker-webserver-1 php public/admin/tool/phpunit/cli/init.php
docker exec -w /var/www/html moodle-docker-webserver-1 vendor/bin/phpunit public/local/microlearning/tests
```
