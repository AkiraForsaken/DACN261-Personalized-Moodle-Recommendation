# Moodle collector

The first stage of the course pipeline. It exports one Moodle course to CSV
files with read-only SQL, so every later stage works on a fixed, auditable
snapshot instead of a live database:

- **who** the learners are (roster, with a per-learner `is_simulated` label);
- **what** the course contains (activities in course order, quizzes and pass
  marks) and **which Knowledge Components** it teaches, derived from the
  course's own question bank;
- **what learners did**: graded answers per question and KC, quiz attempts,
  page views and other events grouped into sessions, activity completion.

Its output is the input of `knowledge_engine` and `learner_model`. No
benchmark dataset is involved, and nothing is written to Moodle.

## Run

From the repository root, in `.venv` (it needs the `mariadb` driver):

```bash
export MOODLE_DOCKER_DBPASS=...                           # see "Configuration"
python moodle-collector.py --course <id>                  # -> courses/<site>/<id>/data/
python moodle-collector.py --course <id> --learners 7,12  # only these learners' rows
```

Options: `--course` (default `$MOODLE_COURSE_ID`, else the first visible
course), `--site` (workspace site name, default `$MICROLEARNING_SITE` or
`local`), `--output-dir` (write here instead of the workspace's `data/`
folder) and `--learners` (comma-separated Moodle user ids).

With `--learners`, the course structure (activities, KC catalog and maps) is
still exported in full, but every per-learner file (roster, events, attempts,
answers, completion) holds only the named learners' rows. This is what makes a
per-learner refresh fast.

From Python:

- `run_collection(cursor, course_id, output_dir, learners=None)` runs every
  step on a caller-supplied cursor. It has no connection handling, so tests
  and notebooks can drive it with a fake cursor.
- `collect(course_id, site, output_dir, learners)` / `main()` do the full
  connect -> collect -> summarise run.

## Outputs

Written to the course workspace, `courses/<site>/<course_id>/data/`, unless
`--output-dir` or `$MOODLE_OUTPUT_DIR` is set.

| File                                 | Content                                                                                           |
| ------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `learner_roster.csv`                 | Active enrolled participants, including new learners; `is_simulated` marks `SIM-` accounts        |
| `activity_catalog.csv`               | Course modules -> topic + sequence; `pass_fraction` is a quiz's pass mark as a share of its grade; `quiz_role` from the ID number |
| `learning_events.csv`                | Filtered, context-resolved, session-tagged logstore events; `kc_id` when the module has one KC    |
| `learning_sessions.csv`              | One row per continuous learning session                                                           |
| `quiz_attempts.csv`                  | Finished non-preview attempts, normalized, with duration                                          |
| `kc_catalog.csv`                     | Course Knowledge Components (from question categories)                                            |
| `moodle_question_kc_map.csv`         | Question-bank entry -> KC (versioned)                                                             |
| `kc_topic_map.csv`                   | KC -> course topic (from quiz usage)                                                              |
| `kc_activity_map.csv`                | KC -> learning resource (module competency, else course-module ID number) and quizzes (fixed or random slots) |
| `kc_mapping_report.json`             | Source of the activity links; KCs per quiz; competency vs question disagreements; quizzes per role; pool difficulty coverage |
| `question_metadata.csv`              | Question-bank entry -> authored difficulty, item set (check/pool), CEFR level, from question tags |
| `question_answer_events.csv`         | Graded item responses with an explicit item -> KC mapping                                         |
| `question_answer_mapping_report.csv` | Mapped / unmapped KT response counts                                                              |
| `course_modules_completion.csv`      | Activity completion states (may be empty)                                                         |
| `collection_manifest.json`           | Written last: course, `collected_at`, learner filter, role filter                                 |

`collected_at` is taken before the first query, so anything a learner does
during the run is newer than the export.

## Design notes

- No quiz IDs are hard-coded. Topics come from Moodle course sections.
- Knowledge Components belong to the course, not to any benchmark dataset: a
  question category with an idnumber is a KC and its parent category is the KC
  dimension (`collect_course_mappings.py`). `MOODLE_QUESTION_KC_MAPPING` may
  point at a hand-curated crosswalk instead.
- An activity's KC comes from Moodle's **course-module competencies**
  (`competency_modulecomp`; competency ID number = `kc_id`), which teachers
  see and edit in the activity settings. Only in a course without any module
  competency does the fallback apply: a non-quiz module whose ID number is a
  `kc_id`. A quiz's KCs always come from its
  questions; its competency links are only checked against them
  (`kc_mapping_report.json`). Moodle has no per-question competency, so the
  question category stays the question's KC.
- A quiz's KCs include its **random slots**: each slot's category filter
  (`question_set_references.filtercondition`) names one KC category, so a
  practice or check-in quiz maps to its KCs before anyone attempts it. For a
  random (KC, quiz) pair, `question_count` is the number of slots.
- A quiz's **role** is the prefix of its course-module ID number (`CHECK-`,
  `PRACTICE-`, `CHECKIN-`, `REVIEW-`, `CHALLENGE-`), the third authoring
  convention. A quiz without a known prefix is a check quiz, so a course
  without roles needs no change. Difficulty, item set and CEFR level are question tags
  (`ml-difficulty-N`, `ml-check`/`ml-pool`, `ml-cefr-xx`) and only feed
  `question_metadata.csv`; the answer files and `mapping_version` do not
  depend on them.
- Mappings are collected **before** the events, so each logstore event gets
  the `kc_id` of its module when the module concerns exactly one KC (empty for
  a mixed quiz, an overview page or an unmapped module). Per-question
  correctness never comes from the logstore (it holds none); it comes from
  `question_attempts` (`question_answer_events.csv`).
- Question responses use one row per completed, graded item attempt. Questions
  without a KC are counted and excluded rather than guessed from labels.
- Partial credit is kept as a raw fraction and converted to binary correctness
  only at full credit for the KT event stream.
- Events are selected from a documented whitelist and classified into
  behavioural categories (`events.py`), so question interactions are countable
  separately from ordinary page and quiz views.
- Every event is mapped to a `course_module_id` through an explicit resolution
  chain (`context_resolution.py`). The method used is recorded in `cmid_source`,
  so the mapping can be audited rather than trusted blindly.
- Sessions are derived from inter-event gaps (`sessions.py`, default 30 min),
  so engagement time is an explicit, documented estimate.

## Configuration

Environment variables, read at import time (`config.py`):

| Variable                     | Default             | Use                                   |
| ---------------------------- | ------------------- | ------------------------------------- |
| `MOODLE_DOCKER_DBPASS`       | (required)          | Database password                     |
| `MOODLE_DB_HOST` / `_PORT`   | `127.0.0.1` / 3306  | MariaDB host and port                 |
| `MOODLE_DB_NAME` / `_USER`   | `moodle` / `moodle` | Database and user                     |
| `MOODLE_DB_PREFIX`           | `m_`                | Moodle table prefix                   |
| `MOODLE_COURSE_ID`           | first visible       | Course to export                      |
| `MOODLE_OUTPUT_DIR`          | course workspace    | Output folder override                |
| `MOODLE_TZ`                  | `Asia/Ho_Chi_Minh`  | Local timezone                        |
| `MOODLE_MASTERY_THRESHOLD`   | `0.80`              | Mastery threshold                     |
| `MOODLE_SESSION_GAP_SECONDS` | `1800`              | Gap that starts a new session         |
| `MOODLE_INCLUDE_ROLES`       | `student`           | Roles counted as participants         |
| `MOODLE_QUESTION_KC_MAPPING` | unset               | Hand-curated question -> KC crosswalk |

## Module map

| Module                  | Role                                            |
| ----------------------- | ----------------------------------------------- |
| `__init__.py`           | `run_collection()`, `collect()`, `main()`       |
| `config.py`             | Env-driven constants, table-prefix helper `t()` |
| `events.py`             | Event whitelist + `classify_event()`            |
| `sessions.py`           | `assign_sessions()`, `summarize_sessions()`     |
| `context_resolution.py` | Event -> `course_module_id` resolution chain    |
| `db.py`                 | Cursor helpers, participant filter, CSV writer  |
| `collect_*.py`          | One collection routine per output CSV           |
| `test-collector.py`     | Manual database connection check                |
