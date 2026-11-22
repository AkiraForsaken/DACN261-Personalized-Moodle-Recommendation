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


def logreg_baseline(kt_dir: Path) -> dict:
    """Re-fit hardening.py's logistic baseline for one workspace, to show what it learned.

    Same inputs and fitting as hardening.build_predictors (train-fold rates, weights
    fitted on validation learners). Returns its weights, three example predictions,
    and its test AUC next to the AUC hardening.py stored, which must agree.
    """
    import json

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    from hardening import LOGREG_INPUTS, logreg_features, train_statistics, unpadded
    from kt_common import TRAIN_FOLD, VALID_FOLD

    kt_dir = Path(kt_dir)
    cfg = next(iter(json.loads((kt_dir / "data_config.json").read_text()).values()))
    dpath = kt_dir / cfg["dpath"]
    tv = pd.read_csv(dpath / cfg["train_valid_file"])
    te = pd.read_csv(dpath / cfg["test_file"])
    qmap = pd.read_csv(kt_dir / cfg["mapping_dir"] / "question_map.csv").sort_values("questions")
    g, kc_mean, item_mean, _ = train_statistics(
        tv[tv["fold"] == TRAIN_FOLD], qmap["concepts"].to_numpy(), cfg["num_q"], cfg["num_c"])

    def table(rows):
        X, y = [], []
        for _, row in rows.iterrows():
            q, c, r = unpadded(row)
            cs = np.cumsum(r)
            for t in range(1, len(r)):
                X.append(logreg_features(q, c, r, t, item_mean, kc_mean, g, cs)); y.append(r[t])
        return np.array(X), np.array(y)

    Xv, yv = table(tv[tv["fold"] == VALID_FOLD])
    lr = LogisticRegression(C=1.0, max_iter=1000).fit(Xv, yv)
    Xt, yt = table(te)
    auc = float(roc_auc_score(yt, lr.predict_proba(Xt)[:, 1]))
    hm = pd.read_csv(kt_dir / "artifacts/evaluation/hardening_metrics.csv")
    stored = float(hm.loc[hm["predictor"].str.startswith("logreg"), "AUC"].iloc[0])

    def predict(item_rate, kc_rate, learner_rate, answers_so_far):
        lg = lambda p: np.log(p / (1 - p))  # noqa: E731
        z = lr.intercept_[0] + lr.coef_[0] @ [lg(item_rate), lg(kc_rate), lg(learner_rate), np.log1p(answers_so_far)]
        return float(1 / (1 + np.exp(-z)))

    skill = round(float(g), 2)  # an average skill: the train-fold correct rate
    examples = pd.DataFrame([
        {"question": "easy (85% correct)", "learner so far": "strong (80%)", "P(correct)": predict(.85, skill, .80, 20)},
        {"question": "hard (35% correct)", "learner so far": "strong (80%)", "P(correct)": predict(.35, skill, .80, 20)},
        {"question": "hard (35% correct)", "learner so far": "weak (40%)", "P(correct)": predict(.35, skill, .40, 20)},
    ])
    weights = pd.DataFrame({"input": LOGREG_INPUTS, "weight": lr.coef_[0]})
    return {"weights": weights, "intercept": float(lr.intercept_[0]), "examples": examples,
            "skill_rate": skill, "n_valid": len(yv), "n_test": len(yt),
            "test_auc": auc, "stored_auc": stored}
