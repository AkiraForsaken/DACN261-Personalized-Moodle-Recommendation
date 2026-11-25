"""
Hardening analysis on the held-out test fold (script version of the
"HARDENING ANALYSES" cell of the archived notebook 02_deep_kt_training_and_evaluation.ipynb).

Reads the prediction files written by evaluate.py and adds, on exactly the same
targets (every answer of a test learner except the first):

  baselines  kc_mean, item_mean (train-fold, smoothed), logistic regression on
             item + KC + learner-so-far (fitted on VALIDATION learners), and the
             same logistic model plus PFA-style per-KC counts of the learner's
             earlier correct and incorrect answers
  BKT        default  = the course's fixed parameters (knowledge_engine.config)
             fitted   = per-KC grid-search MLE on the TRAIN fold
  deep KT    each model's probability averaged over its seeds

and reports AUC / accuracy / log loss / Brier / ECE, learner-bootstrap 95 %
intervals and paired AUC differences. Nothing is trained except the BKT grid
fit and the logistic regression.

    KT_COURSE_DIR=data/benchmarks/ednet_kt1_5000/kt \
        python knowledge_tracing/hardening.py --dataset-name ednet_kt1

For english_kt the curated predictions in knowledge_tracing/artifacts/ are
only READ; the report goes to data/benchmarks/english_kt/evaluation/, so
nothing under knowledge_tracing/artifacts/ is written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluate import (
    EPS,
    metrics_for,
    parse_predictions,
    seq_lists,
    to_markdown,
)  # noqa: E402
from kt_common import (  # noqa: E402
    DATASET_NAME,
    PROJECT_ROOT,
    TRAIN_FOLD,
    VALID_FOLD,
    dataset_artifacts_dir,
    is_benchmark,
    load_data_config,
)

from knowledge_engine.bkt import (  # noqa: E402
    DEFAULT_PARAMS,
    fit_bkt_params,
    next_answer_predictions_by_kc,
)

N_BOOT = 1000
BOOT_SEED = 7
M_SMOOTH = 5.0
PAIRS = [
    ("AKT", "DKT"),
    ("AKT", "SAKT"),
    ("DKT", "SAKT"),
    ("AKT", "logreg"),
    ("DKT", "logreg"),
    ("SAKT", "logreg"),
    ("AKT", "BKT fitted"),
    ("DKT", "BKT fitted"),
    ("BKT fitted", "BKT default"),
    ("BKT fitted", "kc_mean"),
    ("AKT", "item_mean"),
    # Extra benchmark models; a pair is skipped when either side is absent.
    ("AKT", "SAINT"),
    ("AKT", "DKVMN"),
    ("SAINT", "logreg"),
    ("DKVMN", "logreg"),
    ("SAINT", "SAKT"),
    ("DKVMN", "DKT"),
    ("AKT", "logreg+KC counts"),
    ("logreg+KC counts", "logreg item"),
]


def unpadded(row):
    mask = seq_lists(row, "selectmasks")
    n = sum(1 for v in mask if v != -1)
    return (
        np.array(seq_lists(row, "questions")[:n]),
        np.array(seq_lists(row, "concepts")[:n]),
        np.array(seq_lists(row, "responses")[:n]),
    )


def _logit(p):
    return np.log(p / (1 - p))


# Names of the logistic baseline's inputs, in column order of logreg_features().
LOGREG_INPUTS = [
    "question difficulty (logit of item mean)",
    "skill difficulty (logit of KC mean)",
    "learner so far (logit of running accuracy)",
    "position (log 1 + answers so far)",
]


def train_statistics(train_rows, item_kc, num_q: int, num_c: int):
    """Train-fold correct rates: global, per KC and per item (smoothed towards the KC)."""
    item_sum, item_cnt = np.zeros(num_q), np.zeros(num_q)
    kc_sum, kc_cnt = np.zeros(num_c), np.zeros(num_c)
    kc_sequences: dict[int, list] = {}
    for _, row in train_rows.iterrows():
        q, c, r = unpadded(row)
        np.add.at(item_sum, q, r)
        np.add.at(item_cnt, q, 1)
        np.add.at(kc_sum, c, r)
        np.add.at(kc_cnt, c, 1)
        for kc in np.unique(c):
            kc_sequences.setdefault(int(kc), []).append(r[c == kc].tolist())
    global_mean = kc_sum.sum() / kc_cnt.sum()
    kc_mean = (kc_sum + M_SMOOTH * global_mean) / (kc_cnt + M_SMOOTH)
    item_mean = (item_sum + M_SMOOTH * kc_mean[item_kc]) / (item_cnt + M_SMOOTH)
    return global_mean, kc_mean, item_mean, kc_sequences


def logreg_features(q, c, r, t: int, item_mean, kc_mean, global_mean, csum=None):
    """The logistic baseline's four inputs for answer t of one learner (uses answers < t only)."""
    csum = np.cumsum(r) if csum is None else csum
    running = (csum[t - 1] + 2 * global_mean) / (t + 2)
    return [
        _logit(item_mean[q[t]]),
        _logit(kc_mean[c[t]]),
        _logit(running),
        np.log1p(t),
    ]


def calibration(y, p, bins=10):
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    rows, ece = [], 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append(
                {
                    "bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}",
                    "n": int(m.sum()),
                    "mean_predicted": round(float(p[m].mean()), 3),
                    "observed_rate": round(float(y[m].mean()), 3),
                }
            )
            ece += m.mean() * abs(p[m].mean() - y[m].mean())
    return pd.DataFrame(rows), float(ece)


def build_predictors(data_cfg: dict, pred_dir: Path, models):
    dpath = Path(data_cfg["dpath"])
    tv = pd.read_csv(dpath / data_cfg["train_valid_file"])
    te = pd.read_csv(dpath / data_cfg["test_file"])
    train_rows, valid_rows = tv[tv["fold"] == TRAIN_FOLD], tv[tv["fold"] == VALID_FOLD]
    # english_kt's curated config predates mapping_dir; its maps sit one level
    # above the pyKT files (data/processed/), as the notebook read them.
    mapping_dir = Path(data_cfg.get("mapping_dir") or dpath.parent)
    qmap = pd.read_csv(mapping_dir / "question_map.csv").sort_values("questions")
    item_kc = qmap["concepts"].to_numpy()
    num_q, num_c = data_cfg["num_q"], data_cfg["num_c"]

    # ---- train-fold statistics ------------------------------------------------
    global_mean, kc_mean, item_mean, kc_sequences = train_statistics(
        train_rows, item_kc, num_q, num_c
    )

    print(f"Fitting per-KC BKT on {len(kc_sequences)} train-fold KCs ...", flush=True)
    bkt_fitted = fit_bkt_params(kc_sequences)

    def target_table(rows, with_bkt=False):
        y, X, learner, b_def, b_fit, K = [], [], [], [], [], []
        for li, (_, row) in enumerate(rows.iterrows()):
            q, c, r = unpadded(row)
            csum = np.cumsum(r)
            if with_bkt:
                pd_ = next_answer_predictions_by_kc(
                    c.tolist(), r.tolist(), {}, DEFAULT_PARAMS
                )
                pf_ = next_answer_predictions_by_kc(
                    c.tolist(), r.tolist(), bkt_fitted, DEFAULT_PARAMS
                )
            right, wrong = {}, {}
            for t in range(1, len(r)):
                kc = int(c[t - 1])
                if r[t - 1]:
                    right[kc] = right.get(kc, 0) + 1
                else:
                    wrong[kc] = wrong.get(kc, 0) + 1
                X.append(
                    logreg_features(q, c, r, t, item_mean, kc_mean, global_mean, csum)
                )
                # PFA-style: the learner's earlier correct / incorrect answers on this KC.
                K.append([np.log1p(right.get(int(c[t]), 0)), np.log1p(wrong.get(int(c[t]), 0))])
                y.append(r[t])
                learner.append(li)
                if with_bkt:
                    b_def.append(pd_[t])
                    b_fit.append(pf_[t])
        X = np.array(X)
        return (
            np.array(y),
            np.hstack([X, np.array(K)]) if len(X) else X,
            np.array(learner),
            np.array(b_def),
            np.array(b_fit),
        )

    y_valid, X_valid, *_ = target_table(valid_rows)
    y_test, X_test, learner_test, bkt_def, bkt_fit = target_table(te, with_bkt=True)
    n_base = len(LOGREG_INPUTS)
    lr = LogisticRegression(C=1.0, max_iter=1000).fit(X_valid[:, :n_base], y_valid)
    lr_counts = LogisticRegression(C=1.0, max_iter=1000).fit(X_valid, y_valid)
    sigmoid = lambda x: 1 / (1 + np.exp(-x))  # noqa: E731
    predictors = {
        "kc_mean (baseline)": sigmoid(X_test[:, 1]),
        "item_mean (baseline)": sigmoid(X_test[:, 0]),
        "logreg item+KC+learner (baseline)": lr.predict_proba(X_test[:, :n_base])[:, 1],
        "logreg+KC counts (PFA-style baseline)": lr_counts.predict_proba(X_test)[:, 1],
        "BKT default (fixed course params)": bkt_def,
        "BKT fitted (per-KC grid MLE, train fold)": bkt_fit,
    }

    seed_counts = {}
    for model in models:
        files = sorted(pred_dir.glob(f"{model}_seed*_test_predictions.txt"))
        if not files:
            print(f"WARNING: no prediction files for {model}")
            continue
        probs = []
        for f in files:
            ts, ps = parse_predictions(f)
            if not np.array_equal(ts, y_test):
                raise RuntimeError(
                    f"{f.name}: targets do not line up with the test file"
                )
            probs.append(ps)
        seed_counts[model] = len(files)
        label = f"{model.upper()} (mean of {len(files)} seed{'s' if len(files) > 1 else ''})"
        predictors[label] = np.mean(probs, axis=0)

    fitted_summary = pd.DataFrame([{"kc": k, **vars(v)} for k, v in bkt_fitted.items()])
    return y_test, learner_test, len(te), predictors, seed_counts, fitted_summary


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dataset-name", default=DATASET_NAME)
    ap.add_argument("--models", nargs="+", default=["dkt", "akt", "sakt"])
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    ap.add_argument("--output-dir", type=Path, default=None)
    args = ap.parse_args()

    data_cfg = load_data_config(args.dataset_name)
    pred_dir = dataset_artifacts_dir(args.dataset_name) / "evaluation" / "predictions"
    if args.output_dir is not None:
        eval_dir = args.output_dir
    elif is_benchmark(args.dataset_name):
        eval_dir = (
            PROJECT_ROOT / "data" / "benchmarks" / args.dataset_name / "evaluation"
        )
    else:
        eval_dir = dataset_artifacts_dir(args.dataset_name) / "evaluation"
    prefix = "hardening"

    y, learner, n_learners, predictors, seed_counts, fitted = build_predictors(
        data_cfg, pred_dir, args.models
    )

    metric_rows, reliability = [], {}
    for name, p in predictors.items():
        m = metrics_for(y, p)
        table, ece = calibration(y, np.clip(p, EPS, 1 - EPS))
        reliability[name] = table
        metric_rows.append(
            {
                "predictor": name,
                "AUC": round(m["auc"], 4),
                "Accuracy": round(m["accuracy"], 4),
                "Log loss": round(m["log_loss"], 4),
                "Brier": round(float(np.mean((p - y) ** 2)), 4),
                "ECE (10 bins)": round(ece, 4),
            }
        )
    metrics_table = pd.DataFrame(metric_rows)

    idx_by_learner = [np.flatnonzero(learner == i) for i in range(n_learners)]
    rng = np.random.RandomState(BOOT_SEED)
    boot = {name: np.empty(args.n_boot) for name in predictors}
    for b in range(args.n_boot):
        pick = rng.randint(0, n_learners, n_learners)
        ix = np.concatenate([idx_by_learner[i] for i in pick])
        for name, p in predictors.items():
            boot[name][b] = roc_auc_score(y[ix], p[ix])

    auc_ci = pd.DataFrame(
        [
            {
                "predictor": n,
                "AUC": round(roc_auc_score(y, predictors[n]), 4),
                "CI95_low": round(float(np.percentile(boot[n], 2.5)), 4),
                "CI95_high": round(float(np.percentile(boot[n], 97.5)), 4),
            }
            for n in predictors
        ]
    )

    def label(prefix_):
        return next((k for k in predictors if k.startswith(prefix_)), None)

    pair_rows = []
    for a, b in PAIRS:
        na, nb = label(a), label(b)
        if na is None or nb is None:
            continue
        d = boot[na] - boot[nb]
        point = roc_auc_score(y, predictors[na]) - roc_auc_score(y, predictors[nb])
        pair_rows.append(
            {
                "A - B": f"{na.split(' (')[0]} - {nb.split(' (')[0]}",
                "AUC diff": round(point, 4),
                "CI95_low": round(float(np.percentile(d, 2.5)), 4),
                "CI95_high": round(float(np.percentile(d, 97.5)), 4),
                "P(A>B)": round(float((d > 0).mean()), 3),
            }
        )
    pairs_table = pd.DataFrame(pair_rows)

    report = (
        f"# Hardening report: `{args.dataset_name}` (test split)\n\n"
        f"- Test learners: {n_learners}; targets: {len(y):,} (every answer except each learner's first)\n"
        f"- Deep-KT predictions averaged over seeds: {seed_counts}\n"
        f"- Bootstrap: {args.n_boot} resamples of test learners (percentile 95% intervals); "
        "test-learner sampling noise only, not split-to-split or seed variance.\n"
        "- kc_mean / item_mean / BKT-fit use the TRAIN fold; logreg is fitted on VALIDATION learners.\n"
        "- BKT default = the course's fixed, uncalibrated parameters; BKT fitted = per-KC grid MLE "
        f"(KCs with < 30 train answers keep the defaults; {len(fitted)} KCs).\n\n"
        "## Metrics\n\n"
        + to_markdown(metrics_table)
        + "\n\n## AUC with 95% bootstrap interval\n\n"
        + to_markdown(auc_ci)
        + "\n\n## Paired AUC differences (same resampled learners)\n\n"
        + to_markdown(pairs_table)
        + "\n\n## Reliability tables\n\n"
        + "\n\n".join(
            f"**{n}**\n\n" + to_markdown(t)
            for n, t in reliability.items()
            if not n.startswith(("kc_mean", "item_mean"))
        )
        + "\n"
    )
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / f"{prefix}_report.md").write_text(report)
    metrics_table.to_csv(eval_dir / f"{prefix}_metrics.csv", index=False)
    auc_ci.to_csv(eval_dir / f"{prefix}_auc_ci.csv", index=False)
    pairs_table.to_csv(eval_dir / f"{prefix}_paired_auc.csv", index=False)
    fitted.to_csv(eval_dir / f"{prefix}_bkt_fitted_params.csv", index=False)
    (eval_dir / f"{prefix}_summary.json").write_text(
        json.dumps(
            {
                "dataset": args.dataset_name,
                "n_test_learners": n_learners,
                "n_targets": int(len(y)),
                "seed_counts": seed_counts,
                "metrics": metric_rows,
            },
            indent=2,
        )
    )
    print(report[:3000])
    print(f"Saved to {eval_dir}/{prefix}_*")


if __name__ == "__main__":
    main()
