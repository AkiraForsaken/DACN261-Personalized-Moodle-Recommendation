"""
Test AUC versus cohort size for the EdNet-KT1 benchmark.

Collects, for every cohort folder data/benchmarks/ednet_kt1_<N>/kt:
  * deep KT test AUC per seed (artifacts/evaluation/test_comparison.json)
  * BKT default / fitted and the baselines (artifacts/evaluation/hardening_summary.json)
and writes data/benchmarks/ednet_kt1_learning_curve.csv (+ .png).

Cohorts are nested (300 c 1000 c 2000 c 5000) but each has its own 70/10/20
learner split, so the test learners differ between sizes; the curve shows how
much each model gains from more training learners, with that caveat.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = PROJECT_ROOT / "data" / "benchmarks"


def collect(bench_dir: Path, prefix: str) -> pd.DataFrame:
    rows = []
    for kt in sorted(bench_dir.glob(f"{prefix}_*/kt")):
        m = re.fullmatch(rf"{prefix}_(\d+)", kt.parent.name)
        if not m:
            continue
        n = int(m.group(1))
        ev = kt / "artifacts" / "evaluation"
        comp = ev / "test_comparison.json"
        if comp.exists():
            data = json.loads(comp.read_text())
            per_model: dict[str, list] = {}
            for run in data["runs"]:
                per_model.setdefault(run["model"].upper(), []).append(run["auc"])
            for model, aucs in per_model.items():
                s = pd.Series(aucs, dtype=float)
                rows.append({"cohort": n, "model": model, "seeds": len(s),
                             "auc_mean": s.mean(),
                             "auc_sd": s.std(ddof=1) if len(s) > 1 else None,
                             "n_test_learners": data["n_test_learners"],
                             "n_targets": data["n_targets"]})
        hard = ev / "hardening_summary.json"
        if hard.exists():
            data = json.loads(hard.read_text())
            for m_ in data["metrics"]:
                name = m_["predictor"]
                if name.startswith(("BKT", "logreg", "kc_mean", "item_mean")):
                    rows.append({"cohort": n, "model": name.split(" (")[0], "seeds": None,
                                 "auc_mean": m_["AUC"], "auc_sd": None,
                                 "n_test_learners": data["n_test_learners"],
                                 "n_targets": data["n_targets"]})
    return pd.DataFrame(rows).sort_values(["model", "cohort"]) if rows else pd.DataFrame()


def plot(df: pd.DataFrame, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    order = ["AKT", "SAINT", "DKT", "DKVMN", "SAKT", "BKT fitted",
             "logreg item+KC+learner", "BKT default", "kc_mean"]
    for model in [m for m in order if m in set(df["model"])]:
        g = df[df["model"] == model].sort_values("cohort")
        dashed = not model.isupper()
        ax.errorbar(g["cohort"], g["auc_mean"], yerr=g["auc_sd"].fillna(0),
                    marker="o", capsize=3, linestyle="--" if dashed else "-",
                    label=model)
    ax.set_xscale("log")
    ax.set_xticks(sorted(df["cohort"].unique()))
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.set_xlabel("Learners in cohort (70% used for training)")
    ax.set_ylabel("Test AUC (mean ± sd over seeds)")
    ax.set_title("EdNet-KT1: test AUC vs cohort size")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=200)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench-dir", type=Path, default=BENCH_DIR)
    ap.add_argument("--prefix", default="ednet_kt1")
    args = ap.parse_args()
    df = collect(args.bench_dir, args.prefix)
    if df.empty:
        raise SystemExit(f"No evaluated cohorts under {args.bench_dir}/{args.prefix}_*/kt")
    out = args.bench_dir / f"{args.prefix}_learning_curve.csv"
    df.round(4).to_csv(out, index=False)
    print(df.round(4).to_string(index=False))
    print(f"Saved {out}")
    try:
        plot(df, out.with_suffix(".png"))
        print(f"Saved {out.with_suffix('.png')}")
    except ModuleNotFoundError:
        print("matplotlib not installed here; rerun with an environment that has it "
              "for the figure (the CSV is complete).")


if __name__ == "__main__":
    main()
