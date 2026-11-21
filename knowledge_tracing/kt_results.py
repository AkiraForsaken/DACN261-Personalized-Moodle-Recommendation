"""
Read saved KT evaluation outputs into the tables of Chapter 4 (CPU only).

* ``model_summary``: test-fold metrics per model, mean and sd over seeds.
* ``hardening_tables``: the predictor table, AUC intervals and paired differences
  written by hardening.py.
* ``per_answer_frame``: every scored test answer with each model's probability
  (mean over seeds), aligned and checked by analysis.py.
* ``reliability``: reliability diagram (10 bins) from that frame.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from analysis import attach_predictions, scored_targets

LABEL = {"dkt": "DKT", "sakt": "SAKT", "akt": "AKT", "dkvmn": "DKVMN", "saint": "SAINT"}
ORDER = ["dkt", "sakt", "akt", "dkvmn", "saint"]


def model_summary(test_comparison_csv: Path) -> pd.DataFrame:
    t = pd.read_csv(test_comparison_csv)
    g = t.groupby("model")
    out = pd.DataFrame({
        "AUC": g["auc"].mean(),
        "AUC sd": g["auc"].std(),
        "Accuracy": g["accuracy"].mean(),
        "Log loss": g["log_loss"].mean(),
        "valid AUC": g["valid_auc"].mean(),
        "best epoch": g["best_epoch"].mean(),
    })
    out = out.reindex([m for m in ORDER if m in out.index])
    out.index = [LABEL[m] for m in out.index]
    return out.reset_index(names="model")


SHORT = {
    "kc_mean (baseline)": "KC mean",
    "item_mean (baseline)": "Item mean",
    "logreg item+KC+learner (baseline)": "Logistic regression",
    "logreg item+KC+learner": "Logistic regression",
    "BKT default (fixed course params)": "BKT default",
    "BKT fitted (per-KC grid MLE, train fold)": "BKT fitted",
}


def short_name(name: str) -> str:
    name = SHORT.get(name, name)
    return name.replace(" (mean of 3 seeds)", "")


def hardening_tables(eval_dir: Path):
    """Predictor metrics, AUC intervals and paired differences, with short names."""
    eval_dir = Path(eval_dir)
    hm = pd.read_csv(eval_dir / "hardening_metrics.csv")
    ci = pd.read_csv(eval_dir / "hardening_auc_ci.csv")
    paired = pd.read_csv(eval_dir / "hardening_paired_auc.csv")
    hm["predictor"] = hm["predictor"].map(short_name)
    ci["predictor"] = ci["predictor"].map(short_name)
    paired["A - B"] = [" − ".join(short_name(x.strip()) for x in v.split(" - ", 1)) for v in paired["A - B"]]
    return hm, ci, paired


def per_answer_frame(kt_dir: Path, models: list[str]) -> pd.DataFrame:
    """Scored test answers with one probability column per model (mean of its seeds)."""
    kt_dir = Path(kt_dir)
    pred_dir = kt_dir / "artifacts" / "evaluation" / "predictions"
    base = scored_targets(kt_dir)
    out = base.copy()
    for m in models:
        dumps = sorted(pred_dir.glob(f"{m}_seed*_test_predictions.txt"))
        if not dumps:
            continue
        probs = [attach_predictions(base, d, "p")["p"].to_numpy() for d in dumps]
        out[m] = np.mean(probs, axis=0)
    return out


def reliability(pa: pd.DataFrame, models: list[str], bins: int = 10):
    import matplotlib.pyplot as plt

    edges = np.linspace(0, 1, bins + 1)
    rows = []
    fig, ax = plt.subplots(figsize=(5.5, 5.2))
    ax.plot([0, 1], [0, 1], color="grey", ls=":", lw=1)
    for m in models:
        b = np.clip(np.digitize(pa[m], edges) - 1, 0, bins - 1)
        g = pa.assign(b=b).groupby("b").agg(pred=(m, "mean"), obs=("y", "mean"), n=("y", "size"))
        g = g[g["n"] >= 30]
        ax.plot(g["pred"], g["obs"], marker="o", ms=3, label=LABEL.get(m, m))
        ece = float((g["n"] * (g["pred"] - g["obs"]).abs()).sum() / g["n"].sum())
        rows.append({"model": LABEL.get(m, m), "ECE (10 bins)": round(ece, 4)})
    ax.set(xlabel="mean predicted P(correct)", ylabel="observed correct rate",
           title="Reliability (test fold, mean of 3 seeds)", xlim=(0, 1), ylim=(0, 1))
    ax.legend()
    fig.tight_layout()
    return fig, pd.DataFrame(rows)
