# Learner-model data contract

## Inputs

| Input | Source | Required |
|---|---|---|
| `learner_roster.csv` | Moodle collector | no (recommended: lets new learners get profiles) |
| `activity_catalog.csv`, `learning_events.csv`, `quiz_attempts.csv`, `course_modules_completion.csv` | Moodle collector | no (a missing file is an empty table) |
| `kc_catalog.csv`, `kc_topic_map.csv` | Moodle collector (question categories and quiz usage) | no (needed for KC names and skills) |
| `learner_kc_state.csv` | `knowledge_engine` | no (knowledge is `unavailable` without it) |

The learner model reads the knowledge-state file as a contract. It does not
import `knowledge_engine` or `knowledge_tracing`, and does not know which
estimator produced the rows. No benchmark file is read.

## One population per learner

Every output row carries its learner's `source_population`:

- `moodle_collector`: a real learner from the Moodle collector.
- `simulated`: a simulated learner (a Moodle account with a `SIM-` ID number,
  or a learner written by the offline simulator).

The label is read per learner from `is_simulated` in `learner_roster.csv`. A
cohort may therefore mix both: a real student enrolled next to simulated
learners stays `moodle_collector`. `build_manifest.json` then reports
`source_population = "mixed"` and the number of learners of each kind in
`population_counts`. A learner the roster does not label (an export made
before the column existed) takes the directory's label: `simulated` if
`simulation_manifest.json` is present, otherwise `moodle_collector`.

A build fails if the knowledge states label a learner differently from the
data directory, so states computed for one population are never joined to
another. Results for simulated learners demonstrate the pipeline only.

## Learners

A profile is built for every learner who is enrolled at the cutoff (roster),
appears in any collector export before the cutoff, or has a knowledge state.

## Knowledge semantics

`learner_kc_state.csv` keeps one row per learner and KC with its provenance.

- `observed_accuracy`, `attempt_count`: direct evidence from the learner's answers.
- `predicted_correctness`: estimated probability of answering the next question
  on the KC correctly. It is not a mastery probability.
- `p_mastery`: BKT's probability that the KC is known (Phase 1 only).
- `state_status`: `estimated`, or `cold_start` when the learner has no answer on
  the KC. A cold-start value is a prior, not evidence and not zero knowledge.
- `estimator_phase`, `model_name`: 1 / BKT with fixed priors, or 3 / the deep
  model that passed the promotion gate.

## Cutoff

`--as-of` must be Unix seconds or a timezone-aware ISO datetime; naive strings
are rejected. Knowledge states must have been computed with a cutoff at or
before it (their `as_of` column, or the latest `last_attempt_at` for a
full-history run); otherwise the build fails because the estimates would embed
future answers. Re-run `python -m knowledge_engine estimate --as-of ...`.
Cold-start rows have no timestamp and are valid at any cutoff. KC cutoffs are
inclusive; collector frames use strictly-before-cutoff.

## Outputs

- `learner_profile.csv`: one row per learner; component statuses,
  `knowledge_phase`, `knowledge_model`, `profile_completeness`.
- `learner_kc_state.csv`: learner-by-KC states with KC names.
- `learner_skill_summary.csv`, `learner_dimension_summary.csv`: aggregates that
  trace back to KC rows.
- `weak_areas.csv`: one row per learner and KC with a concern status and reason.
- `learner_behavior.csv`, `learner_engagement.csv`, `learner_history.csv`.
- `learner_activity_state.csv`: one row per learner and activity the learner
  touched (no row = not started). `status` is `viewed`, `completed`, `passed`,
  `failed` or `attempted`; with view counts and times, attempt counts, last
  and best score, the quiz's `pass_fraction`, and Moodle's completion state.
  `status_source` says where a quiz outcome comes from: `moodle_completion`
  (Moodle's record, authoritative, used whenever it lies inside the snapshot)
  or `attempts` (best score against the pass mark). The second matters under
  `--as-of`: Moodle keeps one completion row per quiz, stamped with its latest
  change, so a quiz failed before the cutoff and passed after it has no row
  inside the snapshot and is rebuilt as `failed`. `build_manifest.json` counts
  rows where the two disagree (`activity_state.quiz_status_disagreements`).
  The pass mark comes from `pass_fraction` in `activity_catalog.csv`.
- `kc_skill_map.csv`: the KC-to-skill mapping used by this build.
- `build_manifest.json`, `validation_report.md`: provenance and checks. The
  builder stops before writing anything if a check fails.

## Component statuses in `learner_profile.csv`

| Status | Meaning |
|---|---|
| `available` | The component has individual evidence for this learner. |
| `partial` | Available with a known gap (for example engagement without completion tracking). |
| `cold_start_only` | Knowledge, skills or weak areas built from priors only. |
| `no_observations` | The collector has no activity for this learner yet. |
| `unavailable` | The component could not be produced (no knowledge-state file). |

`profile_completeness` lists the components with a usable status
(`available`, `partial`, `cold_start_only`).
