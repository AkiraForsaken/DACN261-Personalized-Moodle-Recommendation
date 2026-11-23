"""
Exploratory analysis of an answer-event log (EdNet-KT1 cohort or the synthetic
English log), shared by the Chapter 4 data notebooks.

Input: the project's answer-event schema (learner_id, item_id, kc_id,
kc_dimension, timestamp, response_time_s, correct, event_index or the row
order), answers only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def answers_only(df: pd.DataFrame) -> pd.DataFrame:
    a = df[df["event_type"] == "answer"] if "event_type" in df else df
    a = a[a["correct"].isin([0, 1])].copy()
    a["correct"] = a["correct"].astype(int)
    order = ["learner_id", "timestamp"] + (["event_index"] if "event_index" in a else [])
    a = a.sort_values(order, kind="stable")
    a["position"] = a.groupby("learner_id").cumcount() + 1
    return a


def unit_stats(a: pd.DataFrame) -> dict[str, pd.Series]:
    """The per-unit quantities that the descriptive table and plots use."""
    by_l = a.groupby("learner_id")
    by_i = a.groupby("item_id")
    by_k = a.groupby("kc_id")
    out = {
        "Answers per learner": by_l.size(),
        "Accuracy per learner": by_l["correct"].mean(),
        "Answers per item": by_i.size(),
        "Accuracy per item": by_i["correct"].mean(),
        "Answers per KC": by_k.size(),
        "Accuracy per KC": by_k["correct"].mean(),
    }
    if "response_time_s" in a and a["response_time_s"].notna().any():
        out["Response time (s)"] = a["response_time_s"].dropna()
    return out


def describe_table(a: pd.DataFrame) -> pd.DataFrame:
    """Descriptive statistics, one column per quantity (count, mean, sd, quartiles)."""
    stats = unit_stats(a)
    rows = ["count", "mean", "std", "min", "25%", "50%", "75%", "max"]
    t = pd.DataFrame({k: v.describe()[rows] for k, v in stats.items()}).T
    t.columns = ["n", "mean", "sd", "min", "Q1", "median", "Q3", "max"]
    t.insert(0, "quantity", t.index)
    return t.reset_index(drop=True)


def overview(a: pd.DataFrame) -> dict:
    lens = a.groupby("learner_id").size()
    return {
        "learners": int(a["learner_id"].nunique()),
        "answers": int(len(a)),
        "items": int(a["item_id"].nunique()),
        "KCs": int(a["kc_id"].nunique()),
        "dimensions": int(a["kc_dimension"].nunique()),
        "correct rate": round(float(a["correct"].mean()), 4),
        "mean answers per learner": round(float(lens.mean()), 2),
        "median answers per learner": float(lens.median()),
    }


def plot_distributions(a: pd.DataFrame, title: str):
    import matplotlib.pyplot as plt

    stats = unit_stats(a)
    keys = [k for k in stats if not k.startswith("Answers per KC") and not k.startswith("Accuracy per KC")]
    fig, axes = plt.subplots(2, 3, figsize=(13, 7))
    for ax, k in zip(axes.flat, keys):
        v = stats[k]
        if k.startswith("Response"):
            v = v[v > 0]
            ax.hist(np.log10(v), bins=50, color="#4C72B0")
            ax.set_xlabel("log10 seconds")
        elif k.startswith("Answers"):
            ax.hist(v, bins=50, color="#4C72B0", log=k.endswith("item"))
            ax.set_xlabel(k.lower())
        else:
            ax.hist(v, bins=40, color="#55A868")
            ax.set_xlabel("share correct")
        ax.set_title(k)
        ax.set_ylabel("count")
    for ax in list(axes.flat)[len(keys):]:
        ax.axis("off")
    fig.suptitle(title)
    fig.tight_layout()
    return fig


def kc_longtail(a: pd.DataFrame):
    import matplotlib.pyplot as plt

    n = a.groupby("kc_id").size().sort_values(ascending=False).to_numpy()
    share = n.cumsum() / n.sum()
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.8))
    ax[0].bar(np.arange(1, len(n) + 1), n, color="#4C72B0", width=1.0)
    ax[0].set_yscale("log")
    ax[0].set(xlabel="KC rank", ylabel="answers (log)", title="Answers per KC")
    ax[1].plot(np.arange(1, len(n) + 1) / len(n), share, color="#C44E52")
    ax[1].set(xlabel="share of KCs (most frequent first)", ylabel="share of answers",
              title="Cumulative share of answers")
    fig.tight_layout()
    top10 = float(share[max(0, int(np.ceil(0.1 * len(n))) - 1)])
    return fig, top10


def by_dimension(a: pd.DataFrame) -> pd.DataFrame:
    g = a.groupby("kc_dimension")
    t = pd.DataFrame({
        "answers": g.size(),
        "items": g["item_id"].nunique(),
        "KCs": g["kc_id"].nunique(),
        "correct rate": g["correct"].mean().round(4),
    })
    if "response_time_s" in a:
        t["median response time (s)"] = g["response_time_s"].median().round(1)
    return t.reset_index()


def plot_by_dimension(a: pd.DataFrame, label: str):
    import matplotlib.pyplot as plt
    import seaborn as sns

    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    order = sorted(a["kc_dimension"].unique())
    rate = a.groupby("kc_dimension")["correct"].mean().reindex(order)
    ax[0].bar(order, rate.values, color="#55A868")
    ax[0].set(ylabel="correct rate", xlabel=label, title=f"Correct rate by {label}")
    ax[0].tick_params(axis="x", rotation=45)
    if "response_time_s" in a and a["response_time_s"].notna().any():
        rt = a[a["response_time_s"] > 0]
        sns.boxplot(data=rt, x="kc_dimension", y="response_time_s", order=order, ax=ax[1],
                    showfliers=False, color="#4C72B0")
        ax[1].set(xlabel=label, ylabel="response time (s)", title="Response time (outliers hidden)")
        ax[1].tick_params(axis="x", rotation=45)
    else:
        ax[1].axis("off")
    fig.tight_layout()
    return fig


def accuracy_by_position(a: pd.DataFrame, width: int = 10, max_pos: int = 200):
    """Correct rate by answer position (bins of ``width``) with a 95% normal CI."""
    import matplotlib.pyplot as plt

    d = a[a["position"] <= max_pos].copy()
    d["bin"] = ((d["position"] - 1) // width) * width + 1
    g = d.groupby("bin")["correct"].agg(["mean", "count"])
    g["ci"] = 1.96 * np.sqrt(g["mean"] * (1 - g["mean"]) / g["count"])
    learners = d.groupby("bin")["learner_id"].nunique()
    fig, ax = plt.subplots(figsize=(8, 3.8))
    ax.errorbar(g.index, g["mean"], yerr=g["ci"], fmt="o-", ms=3, capsize=2, color="#4C72B0")
    ax.set(xlabel=f"answer position (bins of {width})", ylabel="correct rate",
           title="Correct rate by position in the learner's sequence")
    ax2 = ax.twinx()
    ax2.bar(g.index, learners.values, width=width * 0.8, alpha=0.15, color="grey")
    ax2.set_ylabel("learners reaching the position")
    ax2.grid(False)
    fig.tight_layout()
    return fig, g.assign(learners=learners).reset_index()


def learner_features(a: pd.DataFrame) -> pd.DataFrame:
    item_acc = a.groupby("item_id")["correct"].mean()
    a = a.assign(item_acc=a["item_id"].map(item_acc))
    g = a.groupby("learner_id")
    f = pd.DataFrame({
        "answers": g.size(),
        "accuracy": g["correct"].mean(),
        "distinct KCs": g["kc_id"].nunique(),
        "mean item easiness": g["item_acc"].mean(),
    })
    if "response_time_s" in a and a["response_time_s"].notna().any():
        f["median response time"] = g["response_time_s"].median()
    if "timestamp" in a:
        span = g["timestamp"].agg(lambda s: s.max() - s.min())
        f["active days"] = span / (1000 * 86400 if span.max() > 1e11 else 86400)
    return f


def plot_correlation(features: pd.DataFrame, title: str):
    import matplotlib.pyplot as plt
    import seaborn as sns

    c = features.corr(method="spearman")
    fig, ax = plt.subplots(figsize=(6.5, 5.2))
    sns.heatmap(c, annot=True, fmt=".2f", cmap="vlag", vmin=-1, vmax=1, square=True, ax=ax,
                cbar_kws={"shrink": 0.8})
    ax.set_title(title)
    fig.tight_layout()
    return fig, c


def split_coverage(a: pd.DataFrame, splits: pd.DataFrame) -> dict:
    """Leakage check and how much of the test fold is new to the training fold."""
    s = splits.set_index("learner_id")["split"]
    a = a.assign(split=a["learner_id"].map(s))
    if a["split"].isna().any():
        raise ValueError("answers of learners without a split")
    per_learner = a.groupby("learner_id")["split"].nunique()
    train, test = a[a["split"] == "train"], a[a["split"] == "test"]
    tr_items, tr_kcs = set(train["item_id"]), set(train["kc_id"])
    return {
        "train learners": int((s == "train").sum()),
        "valid learners": int((s == "valid").sum()),
        "test learners": int((s == "test").sum()),
        "learners in more than one split": int((per_learner > 1).sum()),
        "test answers": int(len(test)),
        "test answers on items unseen in train": round(float((~test["item_id"].isin(tr_items)).mean()), 4),
        "test items unseen in train": round(float(1 - test["item_id"].drop_duplicates().isin(tr_items).mean()), 4),
        "test answers on KCs unseen in train": round(float((~test["kc_id"].isin(tr_kcs)).mean()), 4),
    }
