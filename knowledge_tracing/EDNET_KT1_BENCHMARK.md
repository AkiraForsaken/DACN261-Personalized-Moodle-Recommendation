# EdNet-KT1 knowledge-tracing benchmark

The project uses EdNet-KT1 to test its choice of knowledge-tracing (KT) model on real English learners. This file records:
- why the benchmark was added and what data it uses;
- what was built;
- how the experiment was run;
- what it found, and where each result appears in the thesis;
- what the results do not show.

Status (2026-11-21): six cohorts (300 to 20,000 learners) × five deep models (DKT, SAKT, AKT, DKVMN, SAINT) × three seeds, all evaluated on the same test targets. DKT/SAKT/AKT ran on Colab (run 1). DKVMN/SAINT ran on Colab (run 2) until it ran out of GPU time in the 20,000 cohort; the four missing runs were finished on a local GTX 1070 Ti. The combined workspaces are in `data/benchmarks/ednet_kt1_<N>/`; the figures and tables come from notebook `notebooks/14_kt_results_ednet.ipynb`; the thesis is Chapter 4 (§4.2 to §4.7, Appendix B).

---

## 1. Why this benchmark exists

The model choice was first justified on `english_kt`, the English Learning Events log. Its documentation describes it as **synthetic**: 1,200 generated learners. The course's own promotion gate runs on **simulated** learners. So no real learner backed the claim that a deep KT model (AKT) is worth using.

EdNet-KT1 fills that gap. It is a public log of real learners of English (TOEIC), in the same domain as the reference course. The benchmark answers three questions (thesis §4.1):

| | Question |
|---|---|
| **E1** | On real learners, how do BKT (default and fitted), DKT, SAKT and AKT rank against non-sequential baselines? |
| **E2** | How does accuracy change as the number of training learners grows, from a course-sized cohort to many thousands? |
| **E3** | Is the course's promotion gate (AKT against BKT on the course's own held-out learners) consistent with E1 and E2? |

OULAD was considered and rejected. It records clicks and assessment scores but not item-level correct or incorrect answers, so it cannot train a KT model.

**Separation rule.** Benchmark weights, KC ids and learner states never reach Moodle. EdNet outputs live only under `data/benchmarks/` and `data/colab/`. Nothing under `knowledge_tracing/configs/` or `knowledge_tracing/artifacts/` was written. The benchmark justifies the *method*; each course still trains and gates its own model.

---

## 2. Data

**Source.** EdNet (Choi et al., AIED 2020), subset KT1, licence CC BY-NC 4.0. Two files are used:

| File | Content used |
|---|---|
| `EdNet-KT1.zip` (1.2 GB, kept zipped) | one CSV per user (784,309 users): `timestamp, solving_id, question_id, user_answer, elapsed_time` |
| `questions.csv` | `correct_answer`, TOEIC `part` (1–7), `tags` |

Both are in `data/external/ednet/`, which is gitignored. Only the sampled users' files are read from the zip; the 3 GB extraction is never done.

**Adapter** `knowledge_tracing/datasets/ednet_kt1.py`. It maps EdNet to the pipeline's answer-event schema.

| Rule | Choice |
|---|---|
| Outcome | correct ⇔ `user_answer == correct_answer` |
| Item | `question_id` |
| KC | **first** tag; untagged questions form one KC per TOEIC part |
| KC dimension | the TOEIC part most frequent among the KC's questions |
| Eligibility | at least 20 answers |
| History | first 200 answers in time order (the pipeline's `MAX_SEQ_LEN`) |
| Sampling | user files shuffled with seed 20240116; the first *N* eligible users form cohort *N* |

**Nesting and reproducibility.**
- Cohorts are nested: the 300 cohort is part of the 1,000 cohort, which is part of the 2,000 cohort, and so on.
- `ednet_manifest.json` records the SHA-256 of both source files, the seed, the filters and per-cohort statistics.
- The current manifest records the 50,000 build (122,239 files read, 72,239 too short). Rebuilding the sample up to 50,000 learners reproduced the 300 to 5,000 files byte for byte, and the 5,000 learners are a subset of the 50,000.
- Building 50,000 eligible users read 122,239 files, so about 41% of users are eligible. The whole eligible pool is therefore about 320,000 learners.

| Cohort | Answers | Items | KCs | Correct rate | Multi-tag items |
|---:|---:|---:|---:|---:|---:|
| 300 | 28,437 | 6,748 | 141 | 0.604 | 46.3% |
| 1,000 | 91,599 | 9,431 | 142 | 0.604 | 45.1% |
| 2,000 | 180,226 | 10,530 | 143 | 0.605 | 45.6% |
| 5,000 | 452,335 | 11,423 | 143 | 0.603 | 46.9% |
| 10,000 | 905,526 | 11,783 | 143 | 0.602 | 47.7% |
| 20,000 | 1,814,348 | 11,955 | 143 | 0.603 | 48.1% |
| 50,000 (built, not trained) | 4,534,633 | 12,119 | 143 | 0.604 | 48.5% |

---

## 3. What was built

| Component | Purpose |
|---|---|
| `knowledge_tracing/datasets/ednet_kt1.py` | EdNet adapter and nested-cohort sampler, described in §2 |
| `knowledge_tracing/prepare_data.py` (edited) | accepts `--dataset-name ednet_kt1` |
| `knowledge_engine/bkt.py` (edited) | adds `fit_bkt_params`, a per-KC BKT grid MLE on the training learners, and `next_answer_predictions_by_kc`. The course's fixed defaults are unchanged. |
| `knowledge_tracing/hardening.py` | script version of the notebook's significance analysis. It adds the BKT default and fitted rows, the logistic and item/KC-mean baselines, ECE and Brier, 1,000-resample learner bootstrap CIs and paired AUC differences. |
| `knowledge_tracing/learning_curve.py` | collects test AUC per cohort size and writes `ednet_kt1_learning_curve.csv` and `.png` |
| `knowledge_tracing/run_ednet_benchmark.sh` | runs every cohort through prepare, pyKT format, training (3 seeds), evaluation, hardening and the learning curve. It is resumable (see below). |
| `knowledge_tracing/train.py` (edited) | `--skip-existing` reuses a run only if its checkpoint exists and its model and training configuration match. A 2-epoch test run cannot stand in for a full run. |
| `knowledge_tracing/evaluate.py` (edited) | **bug fix**: reads pyKT's prediction dump under numpy ≥ 2, which prints `np.float64(...)`. Colab and `.venv-kt` both use numpy 2, so evaluation had been failing. |
| `knowledge_tracing/make_colab_bundle.py` | packs code and cohort CSVs into one zip for Colab. It records git HEAD, uncommitted changes and a SHA-256 for every file. |
| `knowledge_tracing/04_ednet_benchmark_colab.ipynb` | Colab runner: GPU check, Drive, pyKT install (`--no-deps`, so Colab's torch is untouched), 2-minute test run, full run, results view, export |
| `tests/test_ednet_adapter.py`, `tests/test_kt_resume_and_predictions.py` | 8 tests covering the adapter, the BKT fit, the resume rule and the numpy-2 parser. 211 tests pass in both virtualenvs. |

**Resume behaviour (`RESUME=1`, the default).** After a Colab disconnect, rerunning the same cell redoes only the interrupted run:
- a prepared cohort is reused;
- a finished (model, seed) run with the same configuration is reused;
- evaluation is skipped when its outputs are newer than every finished run.

This was tested on three cases:
1. an identical rerun skipped every stage;
2. adding a seed trained only that seed;
3. a run with its summary deleted retrained only that run, and the result was identical to the original.

---

## 4. Protocol

- **Split.** By learner, 70 / 10 / 20 (train / validation / test), seed 20240116. No learner appears in two splits.
- **Model selection.** Validation learners only. The test learners are scored once.
- **Deep models.** pyKT 0.0.38, one fixed configuration for every dataset and cohort (`configs/model_configs.json`). Seeds 42, 43 and 44. At most 50 epochs, early stop after 10 epochs without a validation gain. Best validation AUC kept.
  - DKT and SAKT see KC ids only.
  - **AKT also sees item ids.**
- **Baselines.** All are fitted on the training learners, except the logistic regression, which is fitted on the validation learners.
  - kc_mean: the KC's correct rate.
  - item_mean: the item's correct rate, shrunk towards the KC mean.
  - logistic regression on item difficulty, KC difficulty, the learner's running accuracy and log(1 + attempt number).
  - BKT default: the course's fixed parameters 0.30 / 0.10 / 0.10 / 0.25.
  - BKT fitted: per-KC grid MLE; KCs with fewer than 30 answers keep the defaults.
- **Targets.** Every test answer except each learner's first. All predictors use the same targets.
- **Metrics.** AUC (primary), accuracy, log loss, Brier and 10-bin ECE.
  - Deep models are reported as mean ± sd over the three seeds.
  - Intervals: 1,000 bootstrap resamples of test learners.
  - Model differences are paired: both models are scored on the same resampled learners.

---

## 5. The run

| | |
|---|---|
| Hardware | Google Colab, Tesla T4 (15 GB) |
| Software | Python 3.13.16, PyTorch 2.14.1 (CUDA 13.0), pyKT 0.0.38, NumPy 2.4.6, pandas 2.2.3, scikit-learn 1.6.1 |
| Tier | `extended`: cohorts 300, 1,000, 2,000, 5,000, 10,000, 20,000 |
| Runs | 6 cohorts × 3 models × 3 seeds = 54 trained models |
| Time | 1.84 GPU-hours of training; 2.68 hours wall time in total |

| Cohort | Test learners | Targets | GPU min (9 runs) | Wall min |
|---:|---:|---:|---:|---:|
| 300 | 60 | 5,986 | 1.1 | 2.0 |
| 1,000 | 200 | 19,487 | 4.2 | 6.0 |
| 2,000 | 400 | 35,617 | 6.7 | 9.6 |
| 5,000 | 1,000 | 88,679 | 14.1 | 20.5 |
| 10,000 | 2,000 | 180,813 | 29.7 | 42.4 |
| 20,000 | 4,000 | 364,480 | 54.4 | 80.2 |

**Run 2 (DKVMN, SAINT).** Same Colab environment, notebook `notebooks/archive/05_ednet_benchmark_colab_dkvmn_saint.ipynb`. It stopped during the 20,000 cohort after DKVMN seeds 42–43. Finished locally on 2026-11-21:

- Hardware: GTX 1070 Ti, PyTorch 2.14.1+cu126, pyKT 0.0.38 (`.venv-kt`).
- Inputs: the Drive download (`data/colab/drive_benchmarks/`, DKVMN/SAINT runs with checkpoints) plus run 1's DKT/SAKT/AKT run summaries and prediction dumps (`data/colab/ednet_kt1_results/`). Split, question map and concept map are byte-identical between the two runs for every cohort (SHA-256).
- Command: `DATASET=ednet_kt1 TRAIN_MODELS="dkvmn saint" EVAL_FROM_DUMPS="dkt sakt akt" knowledge_tracing/run_benchmark.sh`. Only DKVMN seed 44 and SAINT seeds 42–44 at 20,000 trained (592, 367, 434, 373 s); every other run was reused.
- DKT/SAKT/AKT checkpoints were not kept, so they are scored from their saved dumps (`evaluate.py --from-dumps`). DKVMN/SAINT were re-scored from their checkpoints.
- Checks: every finished run's files were SHA-256 identical before and after; the 106 re-scored (model, seed) test AUCs match Colab's to within 4.4e-7.

The 50,000 cohort (`max` tier) was not trained. On a local GTX 1070 Ti an AKT epoch took 102 s at that size, so the cohort needs roughly 5 more hours.

---

## 6. Results

### 6.1 Main result: 5,000 learners (thesis Tables 4.2 and 4.3)

| Model | AUC | Accuracy | Log loss |
|---|---|---|---|
| **AKT** | **0.7455 ± 0.0002** | 0.6971 ± 0.0003 | 0.5840 ± 0.0009 |
| DKT | 0.6858 ± 0.0008 | 0.6610 ± 0.0007 | 0.6183 ± 0.0006 |
| SAKT | 0.6701 ± 0.0008 | 0.6537 ± 0.0013 | 0.6270 ± 0.0009 |
| DKVMN | 0.6853 ± 0.0004 | 0.6609 ± 0.0006 | 0.6187 ± 0.0003 |
| SAINT | 0.6656 ± 0.0002 | 0.6536 ± 0.0011 | 0.6282 ± 0.0008 |
| Logistic regression | 0.7321 | 0.6868 | 0.5891 |
| item_mean | 0.7121 | 0.6755 | 0.6030 |
| BKT fitted | 0.6503 | 0.6428 | 0.6368 |
| kc_mean | 0.6310 | 0.6300 | 0.6459 |
| BKT default | 0.5792 | 0.5373 | 0.6964 |
| Constant (train mean) | 0.5000 | 0.6006 | 0.6728 |

The next table uses seed-averaged predictions, with 95% bootstrap intervals:

| Predictor | AUC [95% CI] | Brier | ECE |
|---|---|---|---|
| AKT | 0.748 [0.743, 0.752] | 0.198 | 0.021 |
| Logistic regression | 0.732 [0.727, 0.738] | 0.202 | 0.003 |
| item_mean | 0.712 [0.706, 0.718] | 0.208 | 0.012 |
| DKT | 0.689 [0.683, 0.696] | 0.214 | 0.006 |
| DKVMN | 0.688 [0.682, 0.694] | 0.214 | 0.004 |
| SAKT | 0.675 [0.668, 0.681] | 0.217 | 0.014 |
| SAINT | 0.671 [0.664, 0.678] | 0.218 | 0.014 |
| BKT fitted | 0.650 [0.645, 0.656] | 0.223 | 0.008 |
| kc_mean | 0.631 [0.625, 0.637] | 0.227 | 0.004 |
| BKT default | 0.579 [0.574, 0.584] | 0.249 | 0.112 |

**Two AKT numbers.** 0.7455 is the mean of three models' AUCs; 0.748 is the AUC of their averaged prediction. Averaging reduces noise, so the second is slightly higher. The thesis states which one each table uses.

### 6.2 Learning curve (thesis Figure 4.1 and Table 4.6)

![EdNet-KT1 learning curve](../data/benchmarks/report/figures/ednet_learning_curve.png)

| Test AUC | 300 | 1,000 | 2,000 | 5,000 | 10,000 | 20,000 |
|---|---|---|---|---|---|---|
| AKT | 0.672 | 0.705 | 0.723 | 0.746 | 0.754 | 0.763 |
| DKT | 0.653 | 0.677 | 0.675 | 0.686 | 0.686 | 0.693 |
| SAKT | 0.625 | 0.648 | 0.658 | 0.670 | 0.674 | 0.683 |
| DKVMN | 0.652 | 0.672 | 0.675 | 0.685 | 0.685 | 0.693 |
| SAINT | 0.641 | 0.655 | 0.654 | 0.666 | 0.726 | 0.753 |
| Logistic regression | 0.680 | 0.712 | 0.720 | 0.732 | 0.737 | 0.744 |
| item_mean | 0.649 | 0.692 | 0.699 | 0.712 | 0.718 | 0.725 |
| BKT fitted | 0.642 | 0.650 | 0.646 | 0.650 | 0.649 | 0.649 |
| kc_mean | 0.621 | 0.636 | 0.627 | 0.631 | 0.630 | 0.630 |
| BKT default | 0.592 | 0.580 | 0.580 | 0.579 | 0.578 | 0.579 |

### 6.3 Paired differences (thesis Table 4.7)

| Cohort (train) | AKT − Logistic | AKT − BKT fitted | AKT − DKT | BKT fitted − default |
|---|---|---|---|---|
| 300 (210) | −0.004 [−0.023, 0.012] | +0.035 [0.014, 0.054] | +0.016 [−0.003, 0.033] | +0.049 [0.035, 0.067] |
| 1,000 (700) | −0.001 [−0.006, 0.004] | +0.061 [0.051, 0.071] | +0.029 [0.023, 0.036] | +0.070 [0.059, 0.081] |
| 2,000 (1,400) | **+0.007 [0.004, 0.010]** | +0.081 [0.073, 0.089] | +0.048 [0.042, 0.054] | +0.066 [0.059, 0.074] |
| 5,000 (3,500) | +0.016 [0.013, 0.018] | +0.097 [0.092, 0.102] | +0.058 [0.054, 0.062] | +0.071 [0.066, 0.076] |
| 10,000 (7,000) | +0.019 [0.017, 0.020] | +0.106 [0.103, 0.109] | +0.066 [0.064, 0.069] | +0.072 [0.069, 0.075] |
| 20,000 (14,000) | +0.020 [0.019, 0.021] | +0.116 [0.113, 0.118] | +0.069 [0.067, 0.071] | +0.071 [0.068, 0.073] |

Deep models against each other (thesis table `tab:ednet-paired-by-cohort`):

| Cohort | AKT − SAINT | AKT − DKVMN | SAINT − SAKT | DKVMN − DKT |
|---|---|---|---|---|
| 300 | +0.029 [0.014, 0.046] | +0.012 [0.001, 0.023] | +0.012 [−0.017, 0.039] | +0.004 [−0.013, 0.020] |
| 1,000 | +0.052 [0.042, 0.062] | +0.033 [0.026, 0.041] | +0.004 [−0.009, 0.018] | −0.004 [−0.009, 0.001] |
| 2,000 | +0.070 [0.063, 0.078] | +0.049 [0.043, 0.056] | −0.006 [−0.013, −0.000] | −0.001 [−0.005, 0.002] |
| 5,000 | +0.077 [0.072, 0.082] | +0.059 [0.056, 0.064] | −0.004 [−0.008, −0.000] | −0.001 [−0.003, 0.000] |
| 10,000 | +0.015 [0.014, 0.016] | +0.068 [0.065, 0.071] | +0.062 [0.059, 0.065] | −0.002 [−0.003, −0.001] |
| 20,000 | +0.007 [0.007, 0.008] | +0.070 [0.068, 0.072] | +0.071 [0.069, 0.073] | −0.001 [−0.002, −0.000] |

An interval that excludes 0 means the difference is larger than test-learner sampling noise.

---

## 7. What the results mean

1. **E1: AKT is the most accurate KT model on real learners.** At 5,000 learners it beats every baseline significantly: +0.016 over the logistic baseline and +0.097 over fitted BKT. The synthetic log had suggested that deep models add nothing over a simple model. That holds on real data only for small cohorts.
2. **Much of AKT's lead over DKT and SAKT probably comes from item information.** DKT and SAKT see only the KC, and item_mean alone beats them from 1,000 learners on. No ablation (AKT without item ids) was run, so the split cannot be measured. A fairer test of sequence modelling is AKT against the logistic baseline, because both use item difficulty. That gain is real but modest.
3. **E2: how much data a deep model needs.**
   - Against fitted BKT, AKT wins from the smallest cohort (300 learners, 210 for training).
   - Against the logistic baseline, AKT ties at 300 and 1,000 and leads from 2,000 learners (1,400 for training). The lead levels off at about +0.02 from 10,000 learners.
   - At 300 and 1,000 learners the logistic baseline even has the higher point AUC, so AKT is the most accurate *KT model* in every cohort, not the most accurate predictor.
   - AKT is still improving at 20,000 learners. Fitted BKT is flat from 300 learners: four parameters per KC are already well estimated.
4. **BKT with the course's default parameters is barely informative.** It is the weakest non-constant predictor. AUC is about 0.58 at every size, and its ECE (about 0.11) is the only poor calibration in the table. Fitting per KC adds about +0.07.
5. **Synthetic and real data are consistent at a similar size.** On the synthetic log (1,200 learners), AKT was tied with the logistic baseline (+0.001 [−0.001, 0.004]), and EdNet shows the same tie at 1,000 learners. That is consistent with the synthetic tie being a cohort-size effect, but it does not prove it. The generator's answers depend on item difficulty and ability, which is exactly what the logistic baseline models.
6. **E3: the course gate.** The gate passed AKT against default BKT on 300 simulated learners. That agrees in direction with EdNet at 300 learners. The gate's comparator is the weakest non-constant predictor tested, so passing it shows that AKT beats Phase 1 *on simulated learners*. It does not show that AKT is the best available model at that size, and it says nothing about real learners.
7. **DKVMN and SAINT.** DKVMN tracks DKT at every size (within 0.004). SAINT sees items like AKT but is far behind it up to 5,000 learners (−0.077 at 5,000, below the logistic baseline and slightly below SAKT). It rises to 0.726 at 10,000 (single models still below the logistic baseline's 0.737; the seed-averaged prediction passes it by +0.0035 [0.0018, 0.0050]) and 0.753 at 20,000, within 0.007 of AKT and above the logistic baseline (+0.013). In its untuned configuration, item ids did not help SAINT at small sizes; whether that is data need or configuration was not tested. One difference: SAINT learns a 64-dim embedding per item, AKT one scalar difficulty per item. (pyKT's SAINT reports 1.6M parameters, but it clones the item embedding into each encoder block and only the first block uses it.) On the English log a search moved SAINT by +0.10, so its EdNet numbers may understate it.
8. **Where the models differ (5,000 cohort).** AKT's AUC is 0.73–0.74 in every position bin; the KC-only models and SAINT drop from about 0.69–0.71 (answers 2–30) to 0.61–0.65 (answers 101–200). The first 30 answers are a common initial block (notebook 11), so position is mixed with item mix and learner selection.
9. **Seed variance is negligible.** The seed sd is at most 0.011 (DKVMN at 300 learners) and at most 0.001 from 5,000 learners. Cohort size matters far more than seed.

**Design consequences** (thesis §6.2):
- fit BKT per KC in Phase 1 once enough answers exist;
- make the gate compare against fitted BKT and the logistic baseline, not default BKT;
- the choice of AKT is supported against BKT at every size, and against simple baselines only from about 2,000 learners;
- a course model's calibration must be checked on the course's own held-out learners.

---

## 8. What the results do *not* show

- **Nothing about Moodle learners or learning gains.** EdNet is TOEIC practice in a different app. The results concern predicting the next answer, not recommendation quality or learning.
- **No transferable threshold.** "About 2,000 learners" holds for EdNet's 12k items and 143 KCs. A course with other items, KCs and sequence lengths can differ. The curve gives an order of magnitude; the course's own held-out gate decides.
- **No published EdNet number to compare absolute AUCs with.** The pyKT paper (Liu et al., 2022) evaluates seven other datasets, not EdNet. pyKT's toolkit ships an EdNet preprocessing script, which samples 5,000 users and keeps all tags as a multi-concept question. That differs from the first-tag rule used here. The model ranking AKT > DKT > SAKT matches what pyKT reports at question level on four other datasets.
- **Simplifications.**
  - KC = first tag: 47% of items carry more than one tag.
  - Histories are cut at 200 answers.
  - Users with fewer than 20 answers are excluded.
  - One hyperparameter configuration, not tuned on EdNet.
  - One split per cohort, so the intervals do not cover split-to-split variation.
  - The largest cohort is 20,000 of about 320,000 eligible learners.
- **Unequal inputs.** Only AKT and SAINT see item ids. The logistic baseline partly controls for this, but AKT's margin over DKT should not be read as an architecture effect. The first-tag KC rule may also hurt the KC-only models more than AKT.
- **Intervals.** They cover test-learner sampling only. Seed variation is reported separately as sd.

---

## 9. Where it is in the thesis

| Thesis location | Content |
|---|---|
| §4.1 | E1–E3 |
| §4.2 | EdNet adapter rules, funnel and cohorts, pre-processing, exploratory analysis (first-30-answer block, unseen items) — notebooks 10–11 |
| §4.3 | protocol, early stop, compute (Colab T4; 20,000 DKVMN seed 44 and SAINT on a local GTX 1070 Ti) |
| §4.4, Tables 4.2 and 4.3 | 5,000-learner results, four findings, plausibility check, synthetic-versus-real paragraph |
| §4.5 | learning curve (five models), paired differences incl. deep-model pairs by cohort, findings, threshold caveat |
| §4.6 | E3 consistency paragraph |
| §4.7 | E1–E3 summary, AUC by answer position, threats to validity |
| Abstract, §7.1 | headline sentence |
| §6.1, §6.2, §7.2 | contribution 3; future work (fitted-BKT gate, logistic comparator, 50k and repeated splits, multi-tag); limitations |
| Appendix B (`app:ednet-run`), Appendix C | cohort and compute tables (GPU per run), environment, reproduction commands |

Figures and tables are generated by notebooks 10–15 into `data/benchmarks/report/` and copied to `thesis_docs/reports/DACN_HK261/{figures,tables}/kt/` (`report_utils.sync_to_thesis()`); the thesis `\input`s them.

---

## 10. Reproduce

```bash
# data (not in git): data/external/ednet/EdNet-KT1.zip + questions.csv
.venv-kt/bin/python knowledge_tracing/datasets/ednet_kt1.py --sizes 300 1000 2000 5000 10000 20000 50000

# Local GPU (all five models; resumable)
DATASET=ednet_kt1 knowledge_tracing/run_benchmark.sh
# ... or notebook knowledge_tracing/notebooks/13_kt_training_runner.ipynb

# Google Colab
.venv-kt/bin/python knowledge_tracing/make_colab_bundle.py   # -> data/colab/ednet_colab_bundle.zip
#   upload to Drive as MyDrive/ednet_kt1/ednet_colab_bundle.zip
#   open knowledge_tracing/notebooks/13_kt_training_runner.ipynb on a GPU runtime
#   (the archived 04/05 notebooks are the record of the original runs)

# Tables and figures (CPU): notebooks 10, 11 and 14
.venv-kt/bin/jupyter nbconvert --to notebook --execute --inplace knowledge_tracing/notebooks/14_kt_results_ednet.ipynb
```

Output locations:
- Per-cohort results (five models): `data/benchmarks/ednet_kt1_<N>/kt/artifacts/evaluation/`, which holds `test_comparison.*`, `hardening_report.md`, `hardening_{metrics,auc_ci,paired_auc,bkt_fitted_params}.csv` and per-target `predictions/`.
- Original Colab exports: `data/colab/ednet_kt1_results/` (run 1) and `data/colab/drive_benchmarks/` (run 2).
- Run logs: `data/colab/ednet_kt1_results/logs/` (run 1), `data/benchmarks/ednet_run_20k_local.log` (local completion).
- Model checkpoints: DKVMN/SAINT locally under `data/benchmarks/`; DKT/SAKT/AKT on Drive only (not downloaded).
