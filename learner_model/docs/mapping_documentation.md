# KC and skill mapping

Knowledge Components belong to the course. They are defined in Moodle and
exported by the collector; no benchmark concept map is involved.

## Authoring convention in Moodle

- A question category with an **ID number** is one KC. The ID number is the
  `kc_id`, the category name is the KC name, and the category description is
  the learning objective.
- The **parent category** is the KC dimension (grammar, vocabulary, reading,
  writing, ...). A KC category directly under the top category gets the
  dimension `general`.
- A question sits in one category, so it belongs to at most one KC. Questions
  in categories without an ID number are not mapped and do not take part in
  knowledge estimation; the collector counts them.
- An activity is linked to a KC through Moodle's **competencies**: one
  competency framework (in `ENG-MICRO-KC`: `ENG-MICRO-KCS`) with one
  competency per KC whose **ID number is the `kc_id`**, linked to the course
  and to the KC's activities (_Activity settings > Competencies_). A non-quiz
  activity linked to a KC's competency is that KC's **learning resource** (the
  `Lesson: <KC name>` pages). Teachers see and edit these links in Moodle's
  own UI. Competency ID numbers that match no KC are reported; dimension
  competencies (`DIM-...`) are parents and ignored.
- A quiz's KCs are always the KCs of its questions. Its competency links are
  checked against them; disagreements go to `kc_mapping_report.json`.
- Moodle has no per-question competency, so the question category stays the
  source of a question's KC.
- **Fallback** for a course without any module competency (`ENG-MICRO`,
  course 10): a non-quiz activity whose **course-module ID number** equals a
  `kc_id` is that KC's learning resource. Quiz ID numbers are ignored.

### One activity = one KC

`ENG-MICRO-KC` follows the microlearning rule: each KC has one lesson and one
check quiz holding only that KC's 4-5 questions (`build-course --layout kc`).
`ENG-MICRO` (course 10) mixes about three KCs per practice quiz. Knowledge
tracing works per question in both, because every question has exactly one KC;
the difference is in quiz-level signals. In a mixed quiz a failed attempt does
not say which KC was weak (the recommender falls back to per-KC answer
accuracy), and practising one KC means taking questions on others. In a
one-KC quiz, the pass/fail result and the completion state are evidence about
that KC alone.

## Files the collector writes

| File                         | Content                                                          |
| ---------------------------- | ---------------------------------------------------------------- |
| `kc_catalog.csv`             | `kc_id, kc_name, kc_dimension, learning_objective`               |
| `moodle_question_kc_map.csv` | Question-bank entry to KC, with `mapping_version`                |
| `kc_topic_map.csv`           | KC to course topic, from the quizzes that use the KC's questions |
| `kc_activity_map.csv`        | KC to activities: `role=learn` (the KC's resource, by module competency or ID number, see `mapping_source`) and `role=practice` (quizzes holding `question_count` of the KC's questions) |
| `kc_mapping_report.json`     | Source of the activity links, KCs per quiz, competency vs question disagreements, KCs without lesson or quiz |

`kc_activity_map.csv` is what a recommender uses to turn a weak KC or a failed
quiz into a concrete next step: revise the `learn` resource, then retry a
`practice` quiz.

`mapping_version` is a hash of the question-to-KC mapping, so it changes
exactly when the mapping does. A deep model promoted under one version is not
used after the version changes until it is retrained.

Setting `MOODLE_QUESTION_KC_MAPPING` to a hand-curated CSV overrides the
category convention for courses that cannot follow it.

## Skills

A skill is one dimension within one topic, for example `Grammar: Unit 1`
(`skill_id = grammar|Unit 1`). A KC used by quizzes in two topics belongs to
two skills. A KC with no topic link falls back to its dimension
(`dimension:grammar`). The mapping used by a build is written to
`kc_skill_map.csv`.

A KC that appears in the knowledge states but not in the catalog is kept in
`learner_kc_state.csv` and listed as `unmapped`; it joins no skill.
