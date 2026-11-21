#!/usr/bin/env python3
"""
Build the Chapter 4 notebook series (10-15) from this script, so the notebooks
stay reviewable as code and can be regenerated after a change.

    .venv-kt/bin/python knowledge_tracing/notebooks/build_notebooks.py          # all
    .venv-kt/bin/python knowledge_tracing/notebooks/build_notebooks.py 14 15    # only these
    .venv-kt/bin/jupyter nbconvert --to notebook --execute --inplace knowledge_tracing/notebooks/1[0-5]_*.ipynb

10  EdNet-KT1: data source and pre-processing          (CPU)
11  EdNet-KT1: exploratory data analysis               (CPU)
12  English Learning Events: pre-processing and EDA    (CPU)
13  Training runner, both datasets                     (GPU; local or Colab)
14  EdNet-KT1: results                                 (CPU)
15  English Learning Events: results                   (CPU)
"""

import sys
from pathlib import Path

import nbformat as nbf

# Build only the notebooks named on the command line (default: all). Building
# replaces a notebook with an unexecuted copy, so name only those you will re-run.
ONLY = set(sys.argv[1:])

HERE = Path(__file__).resolve().parent

COLAB_NOTE = """**Where to run.** Locally (recommended): `.venv-kt` with `requirements-analysis.txt`, started from the repository; no GPU needed.
**On Colab (CPU runtime is enough):** upload `ednet_colab_bundle.zip` (built by `knowledge_tracing/make_colab_bundle.py`) to `MyDrive/ednet_kt1/`; the training runs already live in `MyDrive/ednet_kt1/data/benchmarks/` (for the English run, upload `data/benchmarks/english_kt/` there too). Then *Runtime → Run all*: the first cell mounts Drive, unpacks the bundle and links the benchmark folder. Figures and tables are written to `data/benchmarks/report/` (on Drive when on Colab)."""

SETUP = """import sys, os
if "google.colab" in sys.modules:
    !pip install -q seaborn
from pathlib import Path
_root = Path.cwd().resolve()
while not (_root / "knowledge_tracing").is_dir() and _root.parent != _root:
    _root = _root.parent
if "google.colab" in sys.modules and not (_root / "knowledge_tracing").is_dir():
    import zipfile
    from google.colab import drive
    drive.mount("/content/drive")
    import time as _t
    with zipfile.ZipFile("/content/drive/MyDrive/ednet_kt1/ednet_colab_bundle.zip") as zf:
        for _i in zf.infolist():   # keep the zip's timestamps: the runner compares mtimes
            _p = zf.extract(_i, "/content/ednet")
            _m = _t.mktime(_i.date_time + (0, 0, -1))
            os.utime(_p, (_m, _m))
    _root = Path("/content/ednet")
sys.path[:0] = [str(_root), str(_root / "knowledge_tracing")]
import report_utils as ru
ROOT = ru.setup()
ru.style()
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from IPython.display import display, Markdown
pd.set_option("display.width", 160, "display.max_columns", 30)
print("repository:", ROOT)"""


def md(s):
    return nbf.v4.new_markdown_cell(s.strip())


def code(s):
    return nbf.v4.new_code_cell(s.strip())


def write(name, title, cells):
    if ONLY and name not in ONLY:
        return
    nb = nbf.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    nb.cells = [md(title), md(COLAB_NOTE) if name != "13" else md(""), code(SETUP)] + cells
    nb.cells = [c for c in nb.cells if c.source]
    path = next(HERE.glob(f"{name}_*.ipynb"), None)
    slug = {
        "10": "10_ednet_data_preparation.ipynb",
        "11": "11_ednet_eda.ipynb",
        "12": "12_english_data_preparation_and_eda.ipynb",
        "13": "13_kt_training_runner.ipynb",
        "14": "14_kt_results_ednet.ipynb",
        "15": "15_kt_results_english.ipynb",
    }[name]
    out = HERE / slug
    nbf.write(nb, out)
    print("wrote", out)


# --------------------------------------------------------------------------- 10
write("10", """# 10 · EdNet-KT1: data source and pre-processing

Supports thesis Section 4.2 (*Datasets*, EdNet-KT1) and Appendix *EdNet run*.
It documents, with the numbers recomputed from the files, how the public EdNet-KT1 log becomes the answer-event table and the pyKT inputs the models are trained on:

1. the raw data (KT1 user files and the question table);
2. mapping to the project's answer-event schema;
3. sampling: the filter funnel and the nested cohorts;
4. pyKT preparation: chronological sequences, truncation, learner-level split, leakage check.

Nothing here trains a model. Real learners, but benchmark only: no weight, KC or state reaches Moodle.""", [
    md("## 1. Raw data\nEdNet-KT1 holds one CSV per user (`timestamp, solving_id, question_id, user_answer, elapsed_time`). `questions.csv` gives each question's correct option, TOEIC part (1–7) and tags. The 1.2 GB archive is read in place and never extracted; its hash is in the manifest."),
    code("""EDNET = Path("data/external/ednet")
manifest = json.loads((EDNET / "ednet_manifest.json").read_text())
print(json.dumps({k: v for k, v in manifest.items() if k not in ("cohorts", "kt1_zip", "questions")}, indent=1))
questions = pd.read_csv(EDNET / "questions.csv")
display(questions.head())"""),
    code("""raw_zip = EDNET / "EdNet-KT1.zip"
if raw_zip.exists():   # one user file, to show the raw format (local only; Colab has no archive)
    import zipfile
    with zipfile.ZipFile(raw_zip) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".csv"))
        with zf.open(member) as f:
            display(pd.read_csv(f).head(8))
        print(member)
else:
    print("EdNet-KT1.zip not present here; the cohort files below are what the experiments use.")"""),
    md("### Question table: parts and tags\nEach question can carry several tags. The adapter uses the **first** tag as the KC (one KC per question, as pyKT's single-concept setting needs); questions with tag `-1` get one KC per part."),
    code("""q = questions.copy()
q["n_tags"] = q["tags"].astype(str).str.split(";").str.len()
q["first_tag"] = q["tags"].astype(str).str.split(";").str[0]
qt = pd.DataFrame({
    "questions": q.groupby("part").size(),
    "distinct first tags": q.groupby("part")["first_tag"].nunique(),
    "share with >1 tag": q.groupby("part")["n_tags"].apply(lambda s: (s > 1).mean()).round(3),
}).reset_index().rename(columns={"part": "TOEIC part"})
display(qt)
print(f"questions: {len(q):,} | with more than one tag: {(q['n_tags'] > 1).mean():.1%} | untagged (-1): {(q['first_tag'] == '-1').mean():.2%}")
ru.save_table(qt, "ednet_question_parts", "EdNet-KT1 question table by TOEIC part",
              "tab:ednet-question-parts", digits=3)"""),
    md("""## 2. Mapping to the answer-event schema
| Answer-event field | EdNet-KT1 source |
|---|---|
| `learner_id` | user file name (`u<id>`) |
| `item_id` | `question_id` |
| `correct` | 1 if `user_answer` equals the question's `correct_answer`, else 0 |
| `kc_id` | first tag of the question (`untagged_p<part>` if none) |
| `kc_dimension` | most frequent TOEIC part among the KC's questions |
| `timestamp` | `timestamp` (ms) |
| `response_time_s` | `elapsed_time` / 1000 |
| order | time order within the learner; first 200 answers kept |"""),
    code("""ev = pd.read_csv(EDNET / "kt1_events_5000.csv")
display(ev.head())
print(ev.dtypes.to_string())"""),
    md("## 3. Sampling: filter funnel and nested cohorts\nUser files are shuffled with a fixed seed and read in that order; a user is eligible with at least 20 answers. The first *N* eligible users form the cohort of size *N*, so every cohort contains the smaller ones."),
    code("""s = manifest["sampling"]
funnel = pd.DataFrame([
    ["user files in EdNet-KT1", s["user_files"]],
    ["files read (shuffled order, seed %d)" % manifest["seed"], s["files_read"]],
    ["excluded: fewer than %d answers" % manifest["min_answers"], s["ineligible_short"]],
    ["eligible users taken", s["eligible_taken"]],
], columns=["step", "users"])
display(funnel)
print(f"eligible share among files read: {s['eligible_taken'] / s['files_read']:.1%}")
ru.save_table(funnel, "ednet_funnel", "EdNet-KT1 sampling funnel", "tab:ednet-funnel", align="lr")"""),
    code("""coh = pd.DataFrame([{**{"learners": int(k)}, **v} for k, v in manifest["cohorts"].items()])
coh = coh.sort_values("learners")
cohorts = coh[["learners", "answers", "items", "kcs", "positive_rate", "mean_seq_len", "items_multi_tag_share"]].rename(
    columns={"kcs": "KCs", "positive_rate": "correct rate", "mean_seq_len": "answers per learner",
             "items_multi_tag_share": "items >1 tag"})
display(cohorts)
ru.save_table(cohorts, "ednet_cohorts", "EdNet-KT1 nested cohorts (from the sampling manifest)", "tab:ednet-cohort-sizes", digits=3, size="footnotesize")"""),
    code("""# Nesting check: every cohort's learners are a subset of the next larger cohort's.
sizes = sorted(int(k) for k in manifest["cohorts"])
prev = None
for n in sizes:
    ids = set(pd.read_csv(EDNET / f"kt1_events_{n}.csv", usecols=["learner_id"])["learner_id"])
    assert len(ids) == n, (n, len(ids))
    if prev is not None:
        assert prev <= ids, f"cohort {n} does not contain the previous cohort"
    prev = ids
print("nested:", " ⊂ ".join(f"{n:,}" for n in sizes))"""),
    md("## 4. pyKT preparation\n`prepare_data.py` keeps answer events with a binary outcome, orders each learner's answers by time, builds the question and KC index maps, and assigns **learners** (not answers) to train/validation/test (70/10/20, seed 20240116). `init_pykt_dataset.py` writes pyKT's sequence files: the validation fold is the model-selection fold, the test fold is used once, in evaluation."),
    code("""BENCH = ru.ednet_bench_dir()
print("benchmark folder:", BENCH)
rows = []
for n in sizes:
    proc = BENCH / f"ednet_kt1_{n}" / "kt" / "processed"
    if not (proc / "split_assignments.csv").exists():
        continue
    sp = pd.read_csv(proc / "split_assignments.csv")
    assert sp["learner_id"].is_unique, "a learner appears twice"
    c = sp["split"].value_counts()
    rows.append({"learners": n, "train": c.get("train", 0), "valid": c.get("valid", 0), "test": c.get("test", 0),
                 "at the 200-answer cap": int((sp["seq_len"] >= 200).sum())})
splits = pd.DataFrame(rows)
display(splits)
ru.save_table(splits, "ednet_splits",
              "Learner-level split of each EdNet-KT1 cohort (70/10/20)", "tab:ednet-splits")"""),
    code("""pm = json.loads((BENCH / "ednet_kt1_5000/kt/processed/prepare_data_manifest.json").read_text())
print(json.dumps(pm, indent=1)[:2500])"""),
])

# --------------------------------------------------------------------------- 11
write("11", """# 11 · EdNet-KT1: exploratory data analysis

Supports thesis Section 4.2 (*Exploratory analysis*). The main cohort is the 5,000-learner one (the cohort size pyKT's own EdNet setup samples); the split check runs on every cohort.

The questions it answers, and why they matter for the models:
* How long are learners' sequences and how skewed are items and KCs? (cold start; long tail of KCs)
* How do correctness and response time differ by TOEIC part? (KC dimension used in the learner model)
* Does correctness change along a learner's sequence? (the signal KT models exploit)
* How are learner-level quantities related? (Spearman correlations)
* How much of the test fold is new to the training fold? (item-id models such as AKT and SAINT)""", [
    code("""import eda
COHORT = 5000
EDNET = Path("data/external/ednet")
a = eda.answers_only(pd.read_csv(EDNET / f"kt1_events_{COHORT}.csv"))
ov = eda.overview(a)
display(pd.Series(ov).to_frame("value"))"""),
    md("## 1. Descriptive statistics"),
    code("""desc = eda.describe_table(a)
display(desc.round(3))
ru.save_table(desc, "ednet_describe", f"Descriptive statistics, EdNet-KT1 cohort of {COHORT:,} learners",
              "tab:ednet-describe", digits=2, align="lrrrrrrrr", size="footnotesize")"""),
    md("## 2. Distributions"),
    code("""fig = eda.plot_distributions(a, f"EdNet-KT1, {COHORT:,} learners")
ru.save_fig(fig, "ednet_distributions"); plt.show()"""),
    code("""fig, top10 = eda.kc_longtail(a)
ru.save_fig(fig, "ednet_kc_longtail"); plt.show()
print(f"the 10% most frequent KCs carry {top10:.1%} of the answers")"""),
    md("## 3. TOEIC part (KC dimension)"),
    code("""dim = eda.by_dimension(a).rename(columns={"kc_dimension": "TOEIC part"})
display(dim)
ru.save_table(dim, "ednet_by_part", "EdNet-KT1 answers by TOEIC part (5,000 cohort)", "tab:ednet-by-part", digits=3)
fig = eda.plot_by_dimension(a, "TOEIC part"); ru.save_fig(fig, "ednet_by_part"); plt.show()"""),
    md("## 4. Correctness along the sequence\nThe shaded bars show how many learners reach each position: later positions are answered only by the learners who stayed, so the curve mixes learning with selection (longer-staying learners are stronger)."),
    code("""fig, pos = eda.accuracy_by_position(a)
ru.save_fig(fig, "ednet_accuracy_by_position"); plt.show()
display(pos.iloc[[0, 1, 4, 9, 14, 19]].round(4))"""),
    md("### The first 30 answers\nThe position curve steps up after answer 30 and many learners stop at exactly 30 answers. The checks below describe that block: how concentrated its items are and how its TOEIC-part mix differs. The pattern is consistent with a common initial placement block; the data alone cannot say how the app assigned it."),
    code("""lens = a.groupby("learner_id").size()
first, later = a[a["position"] <= 30], a[a["position"] > 30]
top100 = first["item_id"].value_counts().head(100).sum() / len(first)
block = pd.DataFrame([
    ["learners with exactly 30 answers", int((lens == 30).sum())],
    ["learners with exactly 29 / 31 answers", f"{int((lens == 29).sum())} / {int((lens == 31).sum())}"],
    ["share of answers 1-30 on the 100 most common items", round(float(top100), 3)],
    ["learners whose first item is the most common first item", int(a[a["position"] == 1]["item_id"].value_counts().iloc[0])],
    ["correct rate, answer 30 / answer 31", f"{first[first['position'] == 30]['correct'].mean():.3f} / {later[later['position'] == 31]['correct'].mean():.3f}"],
    ["correct rate, answers 1-30 / 31+", f"{first['correct'].mean():.3f} / {later['correct'].mean():.3f}"],
], columns=["check", "value"])
display(block)
mix = pd.concat([first["kc_dimension"].value_counts(normalize=True).rename("answers 1-30"),
                 later["kc_dimension"].value_counts(normalize=True).rename("answers 31+")], axis=1).round(3)
mix = mix.sort_index().reset_index(names="TOEIC part")
display(mix)
ru.save_table(block, "ednet_first_block", "The first 30 answers of each learner (5,000 cohort)", "tab:ednet-first-block", digits=3, align="lr")
ru.save_table(mix, "ednet_first_block_parts", "TOEIC-part mix of answers 1-30 and later answers", "tab:ednet-first-block-parts", digits=3)"""),
    md("## 5. Learner-level correlations (Spearman)"),
    code("""feat = eda.learner_features(a)
fig, corr = eda.plot_correlation(feat, "EdNet-KT1 learner features (Spearman)")
ru.save_fig(fig, "ednet_learner_correlation"); plt.show()"""),
    md("## 6. Split check on every cohort\nNo learner may appear in two folds. The share of test answers on items never answered in the training fold matters for models with an embedding per item (AKT, SAINT): such items keep an untrained embedding."),
    code("""BENCH = ru.ednet_bench_dir()
rows = []
for n in [300, 1000, 2000, 5000, 10000, 20000]:
    sp_path = BENCH / f"ednet_kt1_{n}/kt/processed/split_assignments.csv"
    if not sp_path.exists():
        continue
    an = a if n == COHORT else eda.answers_only(pd.read_csv(EDNET / f"kt1_events_{n}.csv"))
    rows.append({"learners": n, **eda.split_coverage(an, pd.read_csv(sp_path))})
cov = pd.DataFrame(rows)
display(cov)
assert (cov["learners in more than one split"] == 0).all()
ru.save_table(cov[["learners", "test answers", "test answers on items unseen in train", "test items unseen in train"]],
              "ednet_unseen_items", "Test answers on items not seen in the training fold", "tab:ednet-unseen", digits=4)"""),
    code("""print("Summary for the thesis text (5,000 cohort):")
lens = a.groupby("learner_id").size()
print(f"- {ov['learners']:,} learners, {ov['answers']:,} answers, {ov['items']:,} items, {ov['KCs']} KCs; correct rate {ov['correct rate']:.3f}")
print(f"- answers per learner: median {lens.median():.0f}, {(lens >= 200).mean():.1%} truncated at 200")
print(f"- 10% most frequent KCs: {top10:.1%} of answers")
print(f"- correct rate by part: {dim['correct rate'].min():.3f}-{dim['correct rate'].max():.3f}")
print(f"- correct rate, answers 1-30 vs 31+: {first['correct'].mean():.3f} vs {later['correct'].mean():.3f}; {(lens == 30).sum()} learners stop at exactly 30")"""),
])

# --------------------------------------------------------------------------- 12
write("12", """# 12 · English Learning Events (synthetic): pre-processing and EDA

Supports thesis Section 4.2 (*English Learning Events*). **The log is synthetic**: a documented generator produced it, so every pattern below describes the generator, not real learners.

The same steps as notebooks 10–11: raw events → answer events → pyKT inputs, then the same descriptive statistics and plots. It also verifies that the five-model workspace (`data/benchmarks/english_kt/kt`, dataset name `english_bench`) holds **exactly** the split of the curated run in `data/processed/` (which the curated DKT/SAKT/AKT checkpoints were trained on).""", [
    code("""import eda, hashlib
raw = pd.read_csv("data/raw/english_learning_events.csv", encoding="utf-8-sig")
print(f"raw rows: {len(raw):,}")
display(raw.head())"""),
    md("## 1. Raw events\nThe log mixes answers with other learning events. Only answer events with a binary outcome enter the models."),
    code("""et = raw["event_type"].value_counts().rename_axis("event type").reset_index(name="rows")
display(et)
ru.save_table(et, "english_event_types", "English Learning Events: raw event types (synthetic)", "tab:english-events", align="lr")"""),
    code("""a = eda.answers_only(raw)
ov = eda.overview(a)
display(pd.Series(ov).to_frame("value"))
for c in ("hint_count", "switch_count", "change_count"):
    if c in a:
        print(f"{c}: share of answers with >0 = {(a[c] > 0).mean():.3f}")"""),
    md("## 2. Pre-processing and split check"),
    code("""WS = Path("data/benchmarks/english_kt/kt")
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]
checks = []
for f in ["processed/split_assignments.csv", "processed/question_map.csv", "processed/concept_map.csv"]:
    cur, ws = Path("data/processed") / Path(f).name, WS / f
    checks.append({"file": Path(f).name, "curated": sha(cur) if cur.exists() else "missing",
                   "workspace": sha(ws) if ws.exists() else "missing"})
for f in ["train_valid_sequences.csv", "test_sequences.csv"]:
    cur, ws = Path("data/processed/pykt_english") / f, WS / "pykt" / f
    checks.append({"file": f, "curated": sha(cur) if cur.exists() else "missing",
                   "workspace": sha(ws) if ws.exists() else "missing"})
chk = pd.DataFrame(checks); chk["identical"] = chk["curated"] == chk["workspace"]
display(chk)
assert chk["identical"].all(), "the workspace split differs from the curated split"
sp = pd.read_csv(WS / "processed/split_assignments.csv")
cov = eda.split_coverage(a, sp)
display(pd.Series(cov).to_frame("value"))
ru.save_table(pd.DataFrame([cov]).T.reset_index().rename(columns={"index": "check", 0: "value"}),
              "english_split", "English Learning Events: learner-level split (70/10/20)", "tab:english-split", align="lr")"""),
    md("## 3. Descriptive statistics"),
    code("""desc = eda.describe_table(a)
display(desc.round(3))
ru.save_table(desc, "english_describe", "Descriptive statistics, English Learning Events (synthetic)",
              "tab:english-describe", digits=2, align="lrrrrrrrr", size="footnotesize")
fig = eda.plot_distributions(a, "English Learning Events (synthetic)")
ru.save_fig(fig, "english_distributions"); plt.show()"""),
    code("""fig, top10 = eda.kc_longtail(a); ru.save_fig(fig, "english_kc_longtail"); plt.show()
print(f"the 10% most frequent KCs carry {top10:.1%} of the answers")
dim = eda.by_dimension(a).rename(columns={"kc_dimension": "skill"})
display(dim)
ru.save_table(dim, "english_by_dimension", "English Learning Events by skill (synthetic)", "tab:english-by-dim", digits=3)
fig = eda.plot_by_dimension(a, "skill"); ru.save_fig(fig, "english_by_dimension"); plt.show()"""),
    code("""fig, pos = eda.accuracy_by_position(a, width=10, max_pos=200)
ru.save_fig(fig, "english_accuracy_by_position"); plt.show()
feat = eda.learner_features(a)
fig, corr = eda.plot_correlation(feat, "English learner features (Spearman, synthetic)")
ru.save_fig(fig, "english_learner_correlation"); plt.show()"""),
    md("## 4. EdNet-KT1 and English side by side\nThe two logs are not comparable as populations (real vs generated); the table only shows how different the modelling problems are."),
    code("""ed = eda.overview(eda.answers_only(pd.read_csv("data/external/ednet/kt1_events_5000.csv")))
side = pd.DataFrame({"EdNet-KT1 (5,000 cohort)": ed, "English Learning Events": ov}).reset_index(names="")
display(side)
ru.save_table(side, "kt_datasets_side_by_side", "The two benchmark logs", "tab:kt-datasets-gen", digits=3, align="lrr")"""),
])

# --------------------------------------------------------------------------- 13
write("13", """# 13 · Training runner: EdNet-KT1 and English Learning Events

Supports thesis Section 4.3. Runs `knowledge_tracing/run_benchmark.sh` for one dataset: prepare → (optional validation-only search) → train DKT, SAKT, AKT, DKVMN, SAINT × seeds 42/43/44 → evaluate on the test fold → hardening (BKT, logistic regression, bootstrap CIs, paired differences). Everything is resumable: a finished (model, seed) with the same configuration is reused, never retrained.

**Local (recommended):** run from the repository with `.venv-kt` on the GPU.
**Colab:** set `ON_COLAB_DRIVE = True`; it mounts `MyDrive/ednet_kt1`, unpacks `ednet_colab_bundle.zip`, links `data/` to Drive (outputs survive a disconnect) and installs pyKT. If a session runs out of GPU time, run all cells again: only the interrupted run restarts.""", [
    md("## 1. Settings"),
    code("""DATASET = "ednet_kt1"          # "ednet_kt1" | "english_bench"
SIZES = [300, 1000, 2000, 5000, 10000, 20000]   # EdNet cohorts (ignored for english_bench)
SEEDS = [42, 43, 44]
TRAIN_MODELS = ["dkvmn", "saint"]               # models to train; finished runs are reused anyway
EVAL_MODELS = ["dkt", "sakt", "akt", "dkvmn", "saint"]
TUNE_MODELS = []                                # e.g. ["dkvmn", "saint"] for english_bench
EVAL_FROM_DUMPS = ["dkt", "sakt", "akt"] if DATASET == "ednet_kt1" else []   # EdNet run-1 checkpoints were not kept: score their saved predictions
EPOCHS = None                                   # None = configured 50 (early stop after 10 flat epochs)
DRY_RUN = True                                  # True: show status only; set False to train"""),
    md("## 2. Environment"),
    code("""import subprocess, hashlib, time
PY = sys.executable if ru.in_colab() else str(ROOT / ".venv-kt/bin/python")
if ru.in_colab():
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "pykt-toolkit==0.0.38"], check=True)
out = subprocess.run([PY, "-c", "import torch, numpy; from importlib.metadata import version; print('torch', torch.__version__, '| GPU', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none', '| numpy', numpy.__version__, '| pykt-toolkit', version('pykt-toolkit'))"],
                     capture_output=True, text=True)
print(out.stdout or out.stderr)"""),
    md("## 3. Status and protection\nEvery file of a finished run (summary and checkpoint) is fingerprinted before the run; afterwards the runner asserts that none of them changed."),
    code("""BENCH = Path("data/benchmarks")
def workspaces():
    if DATASET == "english_bench":
        return {"english": BENCH / "english_kt/kt"}
    return {n: BENCH / f"ednet_kt1_{n}/kt" for n in SIZES}

def status():
    rows = []
    for key, kt in workspaces().items():
        for m in EVAL_MODELS:
            done = [s for s in SEEDS if (kt / "artifacts" / m / f"seed{s}" / "run_summary.json").exists()]
            rows.append({"workspace": key, "model": m.upper(), "finished seeds": len(done)})
    return pd.DataFrame(rows).pivot(index="workspace", columns="model", values="finished seeds")

def fingerprint():
    fp = {}
    for kt in workspaces().values():
        for f in (kt / "artifacts").glob("*/seed*/*"):
            if f.name in ("run_summary.json", "qid_model.ckpt") and (f.parent / "run_summary.json").exists():
                fp[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return fp

display(status())"""),
    md("## 4. Run"),
    code("""if DRY_RUN:
    print("DRY_RUN is True: nothing trained.")
else:
    before = fingerprint()
    env = dict(os.environ, PY=PY, DATASET=DATASET, SIZES=" ".join(map(str, SIZES)),
               SEEDS=" ".join(map(str, SEEDS)), TRAIN_MODELS=" ".join(TRAIN_MODELS),
               EVAL_MODELS=" ".join(EVAL_MODELS), TUNE_MODELS=" ".join(TUNE_MODELS),
               EVAL_FROM_DUMPS=" ".join(EVAL_FROM_DUMPS))
    if EPOCHS:
        env["EPOCHS"] = str(EPOCHS)
    log = BENCH / f"run_{DATASET}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    t0 = time.time()
    with open(log, "w") as lf:
        p = subprocess.Popen(["bash", "knowledge_tracing/run_benchmark.sh"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in p.stdout:
            lf.write(line)
            if line.startswith(("===", "---")):
                print(line, end="")
        rc = p.wait()
    print(f"exit {rc}, {(time.time() - t0) / 3600:.2f} h, log: {log}")
    after = fingerprint()
    changed = [f for f in before if after.get(f) != before[f]]
    assert not changed, f"finished runs were modified: {changed[:5]}"
    print(f"OK: {len(before)} files of finished runs unchanged; {len(after) - len(before)} new files")
    display(status())"""),
])

# --------------------------------------------------------------------------- 14
RESULTS_COMMON = '''import analysis
from kt_results import model_summary, hardening_tables, per_answer_frame, reliability
M = ru.MODELS
L = ru.MODEL_LABEL'''

write("14", """# 14 · EdNet-KT1: results

Supports thesis Sections 4.4 (results), 4.5 (learning curve) and 4.7 (discussion). Reads only saved outputs (test-fold metrics, hardening, per-answer predictions, training logs); trains nothing.

Every model was trained with one configuration, untuned, three seeds; AUC is on held-out learners. All numbers are recomputed here from the files.""", [
    code(RESULTS_COMMON + '''
BENCH = ru.ednet_bench_dir()
SIZES = [300, 1000, 2000, 5000, 10000, 20000]
MAIN = 5000
print("benchmark folder:", BENCH)'''),
    md("## 1. Test-fold metrics per cohort (mean ± sd over seeds)"),
    code('''tabs = []
for n in SIZES:
    ev = BENCH / f"ednet_kt1_{n}/kt/artifacts/evaluation"
    if (ev / "test_comparison.csv").exists():
        t = model_summary(ev / "test_comparison.csv"); t.insert(0, "learners", n); tabs.append(t)
per_cohort = pd.concat(tabs, ignore_index=True)
display(per_cohort)
main = per_cohort[per_cohort["learners"] == MAIN].drop(columns="learners")
ru.save_table(main, "ednet_main_models", f"Deep KT models on the EdNet-KT1 cohort of {MAIN:,} learners (test fold, 3 seeds)",
              "tab:ednet-main-models", digits=4, size="footnotesize")'''),
    md("## 2. Baselines, intervals and paired differences (hardening)"),
    code('''hm, ci, paired = hardening_tables(BENCH / f"ednet_kt1_{MAIN}/kt/artifacts/evaluation")
display(hm); display(ci); display(paired)
ru.save_table(hm, "ednet_hardening_metrics", f"All predictors on the same test targets ({MAIN:,} cohort; deep models: predictions averaged over 3 seeds)",
              "tab:ednet-hardening", digits=4, size="footnotesize")
ru.save_table(paired, "ednet_paired", f"Paired AUC differences, learner bootstrap ({MAIN:,} cohort)",
              "tab:ednet-paired", digits=4, align="lrrrr")'''),
    md("## 3. Learning curve (test AUC vs cohort size)"),
    code('''lc = pd.read_csv(BENCH / "ednet_kt1_learning_curve.csv")
wide = lc.pivot_table(index="model", columns="cohort", values="auc_mean").round(4)
display(wide)
fig, ax = plt.subplots(figsize=(8.5, 5))
order = ["AKT", "SAINT", "DKT", "DKVMN", "SAKT", "logreg item+KC+learner", "BKT fitted", "BKT default", "kc_mean"]
for name in [o for o in order if o in set(lc["model"])]:
    g = lc[lc["model"] == name].sort_values("cohort")
    deep = name in L.values()
    ax.errorbar(g["cohort"], g["auc_mean"], yerr=g["auc_sd"].astype(float).fillna(0), marker="o" if deep else "s",
                ls="-" if deep else "--", capsize=2, label=name, lw=1.8 if deep else 1.2)
ax.set_xscale("log"); ax.set_xticks(SIZES); ax.set_xticklabels([f"{s:,}" for s in SIZES])
ax.set(xlabel="learners in the cohort (70% train)", ylabel="test AUC", title="EdNet-KT1: test AUC by cohort size")
ax.legend(fontsize=8, ncol=2)
ru.save_fig(fig, "ednet_learning_curve"); plt.show()
curve_tab = wide.reset_index().rename(columns={c: f"{c:,}" for c in wide.columns})
ru.save_table(curve_tab, "ednet_learning_curve", "Test AUC by cohort size (mean of 3 seeds for deep models)",
              "tab:ednet-curve", digits=4, size="footnotesize")'''),
    md("### Paired differences of the deep models, by cohort\nSame learner bootstrap as section 2, per cohort. Cell: difference [95% interval]. An interval that excludes 0 marks a difference larger than test-learner sampling noise (not seed or split variance)."),
    code('''WANT = ["AKT − SAINT", "AKT − DKVMN", "SAINT − SAKT", "DKVMN − DKT", "SAINT − Logistic regression", "DKVMN − Logistic regression"]
rows = []
for n in SIZES:
    ev = BENCH / f"ednet_kt1_{n}/kt/artifacts/evaluation"
    if not (ev / "hardening_paired_auc.csv").exists():
        continue
    pr = hardening_tables(ev)[2].set_index("A - B")
    row = {"learners": n}
    for w in WANT:
        if w in pr.index:
            r = pr.loc[w]
            row[w] = f"{r['AUC diff']:+.3f} [{r['CI95_low']:.3f}, {r['CI95_high']:.3f}]"
    rows.append(row)
pbc = pd.DataFrame(rows)
display(pbc)
ru.save_table(pbc[["learners"] + WANT[:4]], "ednet_paired_by_cohort",
              "Paired AUC differences between deep models by cohort size (learner bootstrap, 95\\\\% interval)",
              "tab:ednet-paired-by-cohort", size="scriptsize")'''),
    md("## 4. Calibration (reliability, mean of 3 seeds)"),
    code('''kt = BENCH / f"ednet_kt1_{MAIN}/kt"
have_pykt = (kt / "pykt/test_sequences.csv").exists()
if have_pykt:
    pa = per_answer_frame(kt, M)
    fig, rel = reliability(pa, [m for m in M if m in pa])
    ru.save_fig(fig, "ednet_reliability"); plt.show()
else:
    print("pykt/test_sequences.csv missing: download the cohort folders from Drive (see notebook 13).")'''),
    md("## 5. Where the models differ: position, TOEIC part, unseen items\nAUC within groups of test answers, from each model's probabilities averaged over its 3 seeds (so slightly above the per-seed means of section 1). *Position* is the answer's index in the learner's sequence: answers 2–10 are the cold-start case the course is in for most learners."),
    code('''if have_pykt:
    mods = [m for m in M if m in pa]
    pa["position bin"] = analysis.position_bin(pa["position"])
    by_pos = analysis.auc_by(pa, mods, "position bin").rename(columns=L)
    by_part = analysis.auc_by(pa, mods, "kc_dimension").rename(columns={**L, "kc_dimension": "TOEIC part"})
    pa["item in train"] = pa["item_seen_in_train"].map({True: "seen", False: "unseen"})
    by_seen = analysis.auc_by(pa, mods, "item in train").rename(columns=L)
    for t in (by_pos, by_part, by_seen): display(t)
    ru.save_table(by_pos, "ednet_auc_by_position", f"Test AUC by answer position ({MAIN:,} cohort; probabilities averaged over 3 seeds)", "tab:ednet-auc-position", digits=4)
    ru.save_table(by_part, "ednet_auc_by_part", f"Test AUC by TOEIC part ({MAIN:,} cohort; probabilities averaged over 3 seeds)", "tab:ednet-auc-part", digits=4)
    ru.save_table(by_seen, "ednet_auc_by_seen", f"Test AUC on items seen or unseen in training ({MAIN:,} cohort; probabilities averaged over 3 seeds)", "tab:ednet-auc-seen", digits=4)'''),
    md("## 6. Training cost and convergence"),
    code('''rows = []
for n in SIZES:
    art = BENCH / f"ednet_kt1_{n}/kt/artifacts"
    if art.exists():
        t = analysis.run_table(art, M); t.insert(0, "learners", n); rows.append(t)
runs = pd.concat(rows, ignore_index=True)
# PyTorch CUDA build recorded in run_summary.json -> machine (colab_environment.json: T4 used cu130)
GPU_OF_BUILD = {"cu130": "T4", "cu126": "1070 Ti"}
cost = runs.groupby(["learners", "model"]).agg(
    epochs=("epochs_run", "mean"), best_epoch=("best_epoch", "mean"),
    s_per_epoch=("seconds_per_epoch", "mean"), minutes_per_run=("train_seconds", lambda s: s.mean() / 60),
    params=("n_params", "first"),
    GPU=("torch_version", lambda v: " + ".join(sorted({GPU_OF_BUILD.get(str(x).rsplit("+", 1)[-1], str(x)) for x in v})))).reset_index()
display(cost.round(2))
ru.save_table(cost[cost["learners"].isin([MAIN, 20000])].round(2).rename(columns={
                  "best_epoch": "best", "s_per_epoch": "s/epoch", "minutes_per_run": "min/run"}), "ednet_cost",
              "Training cost per run (mean of 3 seeds)", "tab:ednet-cost", digits=1, size="footnotesize",
              note="GPU from each run's recorded PyTorch build: T4 = Colab Tesla T4, 1070 Ti = local GTX 1070 Ti. Times on different GPUs are not comparable.")
fig, ax = plt.subplots(figsize=(8, 4))
for m in M:
    log = BENCH / f"ednet_kt1_{MAIN}/kt/artifacts/{m}/seed42/train.log"
    if log.exists():
        c = analysis.training_curve(log); ax.plot(c["epoch"], c["valid_auc"], label=L[m])
ax.set(xlabel="epoch", ylabel="validation AUC", title=f"Validation AUC per epoch (seed 42, {MAIN:,} cohort)")
ax.legend(); ru.save_fig(fig, "ednet_convergence"); plt.show()'''),
    md("## 7. Copy figures and tables into the thesis"),
    code("ru.sync_to_thesis()"),
])

# --------------------------------------------------------------------------- 15
write("15", """# 15 · English Learning Events (synthetic): results

Supports thesis Sections 4.3 (search) and 4.4 (English results). **Synthetic data**: results describe the generator.

1. the validation-only search: DKT/SAKT/AKT from the curated run (read-only), DKVMN/SAINT with the same 8 trials;
2. the five models in one workspace, default configuration (as the curated run kept), 3 seeds;
3. reproduction check: the workspace DKT/SAKT/AKT against the curated checkpoints' test metrics.""", [
    code(RESULTS_COMMON + '''
WS = Path("data/benchmarks/english_kt/kt")
CURATED = Path("knowledge_tracing/artifacts")   # read-only'''),
    md("## 1. Validation-only search (8 trials per model, trial 0 = default)"),
    code('''tun = pd.concat([pd.read_csv(CURATED / "tuning/tuning_results.csv").assign(run="curated"),
                 pd.read_csv(WS / "artifacts/tuning/tuning_results.csv").assign(run="this workspace")], ignore_index=True)
summ = tun.groupby("model", sort=False).apply(lambda g: pd.Series({
    "default (trial 0)": g.loc[g["trial"] == 0, "valid_auc"].iloc[0],
    "best trial": g["valid_auc"].max(),
    "gain": g["valid_auc"].max() - g.loc[g["trial"] == 0, "valid_auc"].iloc[0],
    "mean s / trial": g["seconds"].mean(), "run": g["run"].iloc[0]}), include_groups=False).reset_index()
summ["model"] = summ["model"].map(L)
display(summ)
ru.save_table(summ.drop(columns="run").rename(columns={"default (trial 0)": "default", "mean s / trial": "s / trial"}),
              "english_tuning", "Validation-only search on the English log (8 trials, seed 42; validation AUC)",
              "tab:english-tuning", digits=4, size="footnotesize",
              note="DKT, SAKT and AKT: midterm search; DKVMN and SAINT: same search, this work. The main runs keep the default.")'''),
    md("## 2. Five models, default configuration, test fold"),
    code('''ev = WS / "artifacts/evaluation"
main = model_summary(ev / "test_comparison.csv")
display(main)
ru.save_table(main, "english_main_models", "Deep KT models on the English Learning Events log (synthetic; test fold, 3 seeds)",
              "tab:english-main-models", digits=4, size="footnotesize")
hm, ci, paired = hardening_tables(ev)
display(hm); display(paired)
ru.save_table(hm, "english_hardening_metrics", "All predictors on the English test targets (synthetic; deep models: predictions averaged over 3 seeds)",
              "tab:english-hardening", digits=4, size="footnotesize")
ru.save_table(paired, "english_paired", "Paired AUC differences on the English log (synthetic)",
              "tab:english-paired", digits=4, align="lrrrr")'''),
    md("## 2b. Sensitivity: each model with its best searched configuration\nThe search is selected on validation AUC with seed 42 only. Training each model's best trial with all three seeds in a second workspace (`data/benchmarks/english_kt_searched/`, same split) shows whether the ranking depends on the configuration. On the synthetic log, SAINT's validation AUC moved from 0.66 to 0.81 across its trials, the largest spread of any model."),
    code('''WS2 = Path("data/benchmarks/english_kt_searched/kt")
best_cfg = json.loads((WS2.parent / "best_configs.json").read_text())
display(pd.DataFrame([{"model": L[m], "source": v["_source"], "model_config": json.dumps(v["model_config"]),
                       "training": json.dumps(v["training"])} for m, v in best_cfg.items() if not m.startswith("_")]))
searched = model_summary(WS2 / "artifacts/evaluation/test_comparison.csv")
sens = main[["model", "AUC", "AUC sd"]].merge(searched[["model", "AUC", "AUC sd"]], on="model", suffixes=(" default", " searched"))
sens["change"] = sens["AUC searched"] - sens["AUC default"]
sens["rank default"] = sens["AUC default"].rank(ascending=False).astype(int)
sens["rank searched"] = sens["AUC searched"].rank(ascending=False).astype(int)
display(sens)
ru.save_table(sens[["model", "AUC default", "AUC searched", "change", "rank default", "rank searched"]].rename(
                  columns={"AUC default": "default", "AUC searched": "searched", "rank default": "rank (default)", "rank searched": "rank (searched)"}),
              "english_sensitivity", "English log (synthetic): test AUC with the default and the best searched configuration (3 seeds each)",
              "tab:english-sensitivity", digits=4, size="footnotesize")
_, _, paired2 = hardening_tables(WS2 / "artifacts/evaluation")
display(paired2)'''),
    md("## 3. Reproduction check against the curated run\nSame split and configuration; differences come from GPU nondeterminism and library versions."),
    code('''cur = pd.read_csv(CURATED / "evaluation/test_comparison.csv")
cur = cur[cur["model"].isin(["dkt", "sakt", "akt"])]
mine = pd.read_csv(ev / "test_comparison.csv")
rep = (cur.groupby("model")["auc"].agg(["mean", "std"]).add_prefix("curated ")
       .join(mine.groupby("model")["auc"].agg(["mean", "std"]).add_prefix("workspace ")))
rep["difference"] = rep["workspace mean"] - rep["curated mean"]
rep = rep.reset_index(); rep["model"] = rep["model"].map(L)
display(rep)
ru.save_table(rep, "english_reproduction", "Reproduction of the curated English runs (test AUC over 3 seeds)",
              "tab:english-reproduction", digits=4)'''),
    md("## 4. Calibration, position and cost"),
    code('''pa = per_answer_frame(WS, M)
mods = [m for m in M if m in pa]
fig, rel = reliability(pa, mods); ru.save_fig(fig, "english_reliability"); plt.show()
pa["position bin"] = analysis.position_bin(pa["position"])
by_pos = analysis.auc_by(pa, mods, "position bin").rename(columns=L); display(by_pos)
ru.save_table(by_pos, "english_auc_by_position", "Test AUC by answer position (English, synthetic; probabilities averaged over 3 seeds)", "tab:english-auc-position", digits=4)
runs = analysis.run_table(WS / "artifacts", M)
cost = runs.groupby("model").agg(epochs=("epochs_run", "mean"), best_epoch=("best_epoch", "mean"),
                                 s_per_epoch=("seconds_per_epoch", "mean"), params=("n_params", "first")).reset_index()
display(cost.round(2))'''),
    code("ru.sync_to_thesis()"),
])
