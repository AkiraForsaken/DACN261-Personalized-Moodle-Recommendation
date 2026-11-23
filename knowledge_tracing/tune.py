#!/usr/bin/env python3
"""
Small random hyperparameter search, selected by VALIDATION AUC only.

This is the search of the English benchmark notebook (02, "small random
hyperparameter search"), moved into a module so DKVMN and SAINT get exactly the
same treatment as DKT, SAKT and AKT had: 8 trials per model, trial 0 = the
default configuration, sampling with random.Random(0), seed 42, the test fold
never loaded.

The search is REPORTED, not used to change the main comparison. The curated
DKT/SAKT/AKT runs kept their default configuration although a trial scored
higher (SAKT by +0.0097), so the main five-model runs also use the defaults; the
tuning table shows how much each model gained from the search.

Usage (KT_COURSE_DIR names the workspace):
    KT_COURSE_DIR=data/benchmarks/english_kt/kt \\
        python knowledge_tracing/tune.py --dataset-name english_bench --models dkvmn saint

Writes <workspace>/artifacts/tuning/<model>/trial<i>/{train.log,run_summary.json}
(checkpoints deleted) and <workspace>/artifacts/tuning/tuning_results.csv.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kt_common import (  # noqa: E402
    EMB_TYPE,
    dataset_artifacts_dir,
    is_benchmark,
    load_data_config,
    load_model_configs,
)

N_TRIALS = 8
SEARCH_SEED = 0
TRIAL_SEED = 42

# DKT/SAKT/AKT: the spaces of notebook 02, unchanged. DKVMN/SAINT: their main
# architecture knobs, the same training grid as DKT/SAKT.
TRAIN_GRID = {
    "learning_rate": [2e-3, 1e-3, 5e-4],
    "batch_size": [32, 64],
    "weight_decay": [0.0, 1e-5, 1e-4],
}
SEARCH_SPACE = {
    "dkt": {
        "model": {"emb_size": [64, 128, 256], "dropout": [0.1, 0.2, 0.3, 0.5]},
        "train": TRAIN_GRID,
    },
    "sakt": {
        "model": {
            "emb_size": [64, 128],
            "num_attn_heads": [2, 4, 8],
            "num_en": [1, 2, 3],
            "dropout": [0.1, 0.2, 0.3, 0.4],
        },
        "train": TRAIN_GRID,
    },
    "akt": {
        "model": {
            "d_model": [64, 128],
            "n_blocks": [1, 2, 4],
            "d_ff": [128, 256],
            "dropout": [0.1, 0.2, 0.3],
            "num_attn_heads": [4, 8],
        },
        "train": {
            "learning_rate": [1e-3, 5e-4, 2.5e-4],
            "batch_size": [32, 64],
            "weight_decay": [0.0, 1e-5],
        },
    },
    "dkvmn": {
        "model": {
            "dim_s": [32, 64, 128],
            "size_m": [20, 50, 100],
            "dropout": [0.1, 0.2, 0.3],
        },
        "train": TRAIN_GRID,
    },
    "saint": {
        "model": {
            "emb_size": [64, 128],
            "num_attn_heads": [2, 4, 8],
            "n_blocks": [1, 2, 4],
            "dropout": [0.1, 0.2, 0.3],
        },
        "train": TRAIN_GRID,
    },
}


def sample_trials(model_name: str, n_trials: int, rng: random.Random) -> list:
    """Trial 0 = defaults; then distinct random overrides (notebook 02 logic)."""
    space = SEARCH_SPACE[model_name]
    seen = {json.dumps([{}, {}])}
    trials = [({}, {})]
    while len(trials) < n_trials:
        m = {k: rng.choice(v) for k, v in space["model"].items()}
        t = {k: rng.choice(v) for k, v in space["train"].items()}
        key = json.dumps([m, t], sort_keys=True)
        if key not in seen:
            seen.add(key)
            trials.append((m, t))
    return trials


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dataset-name", required=True)
    ap.add_argument("--models", nargs="+", required=True, choices=sorted(SEARCH_SPACE))
    ap.add_argument("--n-trials", type=int, default=N_TRIALS)
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()
    if is_benchmark(args.dataset_name):
        sys.exit(
            "tune.py writes into the dataset's workspace; the curated english_kt "
            "artifacts are read-only. Use english_bench with KT_COURSE_DIR."
        )

    import pandas as pd
    from train import train_one

    data_cfg = load_data_config(args.dataset_name)
    base = load_model_configs()
    tuning_dir = dataset_artifacts_dir(args.dataset_name) / "tuning"
    rows = []
    for model_name in args.models:
        rng = random.Random(SEARCH_SEED)
        for i, (m_over, t_over) in enumerate(
            sample_trials(model_name, args.n_trials, rng)
        ):
            cfgs = {
                model_name: {
                    "model_config": {**base[model_name]["model_config"], **m_over},
                    "training": {**base[model_name]["training"], **t_over},
                }
            }
            trial_dir = tuning_dir / model_name / f"trial{i}"
            s = train_one(
                model_name,
                TRIAL_SEED,
                None,
                data_cfg,
                cfgs,
                args.dataset_name,
                args.skip_existing,
                run_dir=trial_dir,
            )
            for leftover in (f"{EMB_TYPE}_model.ckpt", "best_model.pth"):
                (trial_dir / leftover).unlink(missing_ok=True)
            rows.append(
                {
                    "model": model_name,
                    "trial": i,
                    "valid_auc": round(s["valid_auc"], 4),
                    "best_epoch": s["best_epoch"],
                    "seconds": s["train_seconds"],
                    "model_config": json.dumps(s["model_config"]),
                    "training": json.dumps(s["training"]),
                }
            )

    out = tuning_dir / "tuning_results.csv"
    new = pd.DataFrame(rows)
    if out.exists():  # keep rows of models not searched in this call
        old = pd.read_csv(out)
        new = pd.concat([old[~old["model"].isin(args.models)], new], ignore_index=True)
    new.to_csv(out, index=False)
    for model_name, g in new.groupby("model", sort=False):
        d = g.loc[g["trial"] == 0, "valid_auc"].iloc[0]
        b = g["valid_auc"].max()
        print(f"{model_name.upper():6s} default {d:.4f} | best trial {b:.4f} | gain {b - d:+.4f}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
