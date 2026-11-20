# Knowledge engine

Produces the learner-by-KC knowledge states of a Moodle course from that
course's own graded answers. It is the only source of knowledge states for
`learner_model`.

## Phases

| Phase          | Estimator in charge                               | Condition                                                           |
| -------------- | ------------------------------------------------- | ------------------------------------------------------------------- |
| 1 Cold start   | Item-level BKT with fixed priors                  | Default. Works with zero answers.                                   |
| 2 Training     | BKT (a deep model is trained but not trusted yet) | Readiness thresholds met; the trained model did not beat BKT.       |
| 3 Personalised | Deep KT model (AKT)                               | The trained model beat BKT on held-out learners of the same course. |

The decision is stored in the course workspace,
`courses/<site>/<course_id>/knowledge/active_model.json`; the course's deep
model lives in its `kt/` folder (see `course_workspace`). Retraining repeats
the whole of Phase 2, including the gate.

## Commands

Run from the repository root. `python knowledge-engine.py <command>` is the
same as `python -m knowledge_engine <command>`.

```bash
python -m knowledge_engine status   --course 10 [--data-dir DIR]
python -m knowledge_engine estimate --course 10 [--data-dir DIR] [--as-of 2026-12-01T00:00:00Z] [--estimator bkt]
python -m knowledge_engine train    --course 10 [--data-dir DIR] [--epochs N] [--force] [--population simulated|moodle_collector]
python -m knowledge_engine simulate [--output-dir data/simulated] [--course-dir data] [--learners 300]
python -m knowledge_engine oracle   --course 10 [--reps 2000]   # simulated learners only, see "Oracle AUC"
```

`estimate` and `status` need only pandas. `train`, and `estimate` once a deep
model is promoted, also need `requirements-kt.txt`.

## Inputs (collector format, in `--data-dir`)

| File                         | Required    | Use                                                    |
| ---------------------------- | ----------- | ------------------------------------------------------ |
| `kc_catalog.csv`             | yes         | The course's Knowledge Components                      |
| `learner_roster.csv`         | no          | Enrolled learners, so new learners get cold-start rows |
| `question_answer_events.csv` | no          | Graded answers; absent or empty means no evidence yet  |
| `moodle_question_kc_map.csv` | for `train` | Question to KC mapping and its version                 |

## Output

`knowledge/knowledge_states/learner_kc_state.csv` in the course workspace (one row per learner and KC;
columns and meaning in `schemas.py`) and `knowledge_state_manifest.json`.

- `predicted_correctness`: probability of answering the next question on the
  KC correctly. Produced by every estimator; the gate compares it.
- `p_mastery`: BKT's probability that the KC is known. Null for the deep model.
- `state_status = cold_start`: no answer on that KC. The row is a prior, not
  evidence about the learner.

## Safeguards

- A promoted model is used only for the population and question mapping it was
  trained on. Otherwise `estimate` falls back to BKT and records why. A model
  trained on simulated learners is therefore never applied to real ones, and a
  changed question bank requires a retrain.
- The population is a property of each learner (`is_simulated` in
  `learner_roster.csv`), not of the data directory. In a cohort that mixes
  real and simulated learners, `estimate` chooses the estimator per
  population: simulated learners can be in Phase 3 while a real learner in the
  same course is in Phase 1. The manifest lists the decision under
  `by_population`.
- `train` uses the answers of one population only and records it as
  `training_population`; real and simulated answers are never pooled. With
  both present the default is `simulated`; pass `--population moodle_collector`
  once the real learners meet the readiness thresholds themselves.
- The gate scores BKT on exactly the prediction targets of the deep model's
  held-out test split and requires a margin (`GATE_MIN_AUC_MARGIN`).
- Simulator output carries `simulation_manifest.json` and `is_simulated = 1`
  on every roster row; everything derived from those learners is labelled
  `source_population = simulated`. The simulator refuses to write into a
  directory holding collector data.
- Simulated learners can also be injected into Moodle itself
  (`python -m moodle_setup simulate-learners`, see
  [moodle_setup/README.md](../moodle_setup/README.md)). They reach the engine
  through the collector, which writes the same manifest when an export holds
  `SIM-` accounts. `train` records the generator's AUC over *all* its answers
  (`oracle_auc_all_answers`, from `simulation_plan_manifest.json`); it is not
  comparable with the gate's AUCs, which cover the held-out targets only.

## Oracle AUC (simulated learners)

```bash
python moodle-setup.py annotate-plan --plan courses/local/10/data/plan_300.json   # once per plan
python knowledge-engine.py oracle --course 10                                      # -> kt/artifacts/evaluation/oracle_report.md
```

For simulated learners the generator's probability P(correct | every hidden
parameter) is the Bayes-optimal score: ranking by it dominates any score built
from less information (Neyman-Pearson), so no model's *expected* AUC exceeds
it **on the same answers**. `oracle` therefore scores the oracle, the selected
deep model and BKT on exactly the gate's 4,985 held-out targets
(`phase.deep_test_targets`), with 95 % intervals from a bootstrap over the
held-out learners and for the paired gaps, plus the oracle's own spread when
outcomes are redrawn from its probabilities. It refuses to report silently on
inconsistent inputs; the report lists four checks:

| Check | What it proves |
| --- | --- |
| `plan_matches_moodle` | every planned answer is in Moodle with the same outcome, and nothing else |
| `targets_match_answers` | each target's question and response equal the Moodle answer it is tied to; the prediction dump agrees |
| `oracle_all_answers_reproduced` | the probabilities are the ones the plan was drawn with (all-answer AUC = recorded value) |
| `gate_reproduced` | deep and BKT AUC recomputed here equal the gate record |

`annotate-plan` regenerates a plan from its seed and the course structure,
requires it to be identical to the stored plan, then adds `p_correct` per
answer and `moodle_userid` per learner. New plans record `p_correct` and all
generator settings directly.

Course 10, 2026-11-18 (seed-7 plan, AKT seed 44):

| Score | AUC on 4,985 targets | 95 % CI |
| --- | --- | --- |
| oracle | 0.7997 | 0.776-0.822 |
| AKT | 0.6966 | 0.665-0.725 |
| BKT | 0.6556 | 0.634-0.676 |

Paired gaps: AKT - BKT 0.015 to 0.066; oracle - AKT 0.088 to 0.120. AKT
reaches 66 % of the oracle's headroom above 0.5, BKT 52 %. Suggested wording:
"On the same held-out answers, the generator's own probabilities reach an AUC
of 0.80 [0.78, 0.82]; AKT reaches 0.70 [0.66, 0.73] and BKT 0.66 [0.63, 0.68]."
The earlier "ceiling 0.798" was computed over all 25,767 answers and should
not be put next to the gate's numbers.

## Limits to state when reporting results

- The oracle AUC exists for simulated learners only. It bounds what the
  models could reach *on this generator*; for real learners there is no such
  bound, and none may be reported.

- BKT parameters are fixed configuration values, not fitted to the course.
- Readiness thresholds and the gate margin are configuration choices.
- The simulator's answer model is not BKT, so the gate is not rigged, but
  results on simulated learners show only that the pipeline works. They are
  not evidence that the estimates are valid for real learners.
