#!/usr/bin/env python3
"""
Per-answer analysis of saved KT test predictions (CPU only, no retraining).

pyKT's prediction dump has one line per test sequence, in the order of the
workspace's pykt/test_sequences.csv, with the KC ids and targets but not the
question ids. This module re-attaches each target to its learner, position,
question and KC by aligning the dump with test_sequences.csv, and checks that
the alignment is exact (same KCs and same targets) before using it.

Used by the Chapter 4 result notebooks for:
  * AUC by answer position (cold start: answers 2-10; EdNet opening block: 11-30; later answers),
  * AUC by KC dimension (TOEIC part, or the English skill),
  * AUC on items seen vs unseen in the training fold,
  * training curves and seconds per epoch from each run's train.log.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

_NP_SCALAR = re.compile(r"np\.(?:float|int)(?:16|32|64)\(([^()]*)\)")
POSITION_BINS = [(2, 10), (11, 30), (31, 100), (101, 200)]  # 30: the common opening block in EdNet (notebook 11)


def _ints(cell) -> list[int]:
    return [int(v) for v in str(cell).split(",")]


def read_dump(path: Path) -> list[tuple[list[int], list[int], list[float]]]:
    """One (next_kcs, targets, probabilities) tuple per test sequence."""
    out = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        rec = ast.literal_eval(_NP_SCALAR.sub(r"\1", line).replace("nan", "None"))
        out.append((list(rec[2]), list(rec[3]), list(rec[4])))
    return out


def scored_targets(kt_dir: Path) -> pd.DataFrame:
    """Every scored test answer: one row per (sequence, position >= 2)."""
    kt_dir = Path(kt_dir)
    te = pd.read_csv(kt_dir / "pykt" / "test_sequences.csv")
    qmap = pd.read_csv(kt_dir / "processed" / "question_map.csv")
    cmap = pd.read_csv(kt_dir / "processed" / "concept_map.csv")
    tv = pd.read_csv(kt_dir / "pykt" / "train_valid_sequences.csv")
    seen = set()
    for q in tv.loc[tv["fold"] == 0, "questions"]:
        seen.update(v for v in _ints(q) if v >= 0)
    dim_by_c = dict(zip(cmap["concepts"], cmap["kc_dimension"])) if "kc_dimension" in cmap else {}
    if not dim_by_c:
        dim_by_c = qmap.drop_duplicates("concepts").set_index("concepts")["kc_dimension"].to_dict()
    item_by_q = dict(zip(qmap["questions"], qmap["item_id"]))

    rows = []
    for seq_no, r in enumerate(te.itertuples(index=False)):
        qs, cs, rs = _ints(r.questions), _ints(r.concepts), _ints(r.responses)
        mask = _ints(r.selectmasks)
        valid = [i for i, m in enumerate(mask) if m != -1]
        for i in valid[1:]:  # pyKT predicts every answer after the first
            rows.append(
                {
                    "seq": seq_no,
                    "uid": int(r.uid),
                    "position": i + 1,
                    "question": qs[i],
                    "item_id": item_by_q.get(qs[i]),
                    "concept": cs[i],
                    "kc_dimension": dim_by_c.get(cs[i]),
                    "y": rs[i],
                    "item_seen_in_train": qs[i] in seen,
                }
            )
    return pd.DataFrame(rows)


def attach_predictions(targets: pd.DataFrame, dump_path: Path, name: str) -> pd.DataFrame:
    """Add column ``name`` (probabilities) after checking the alignment is exact."""
    dump = read_dump(dump_path)
    n_seq = targets["seq"].nunique()
    if len(dump) != n_seq:
        raise ValueError(f"{dump_path}: {len(dump)} sequences, test file has {n_seq}")
    probs = []
    for seq_no, (kcs, ys, ps) in enumerate(dump):
        part = targets[targets["seq"] == seq_no]
        if list(part["y"]) != list(ys) or list(part["concept"]) != list(kcs):
            raise ValueError(f"{dump_path}: sequence {seq_no} does not align with test_sequences.csv")
        probs.extend(ps)
    out = targets.copy()
    out[name] = np.asarray(probs, dtype=float)
    return out


def _auc(y, p) -> float:
    y = np.asarray(y)
    return float(roc_auc_score(y, p)) if 0 < y.sum() < len(y) else float("nan")


def auc_by(df: pd.DataFrame, models: list[str], by: str) -> pd.DataFrame:
    """AUC of each model column within each group of ``by`` (plus n and correct rate)."""
    rows = []
    for key, g in df.groupby(by, dropna=False, observed=True):
        row = {by: key, "n": len(g), "correct_rate": round(float(g["y"].mean()), 4)}
        for m in models:
            row[m] = round(_auc(g["y"], g[m]), 4)
        rows.append(row)
    return pd.DataFrame(rows)


def position_bin(pos: pd.Series) -> pd.Series:
    labels = [f"{a}-{b}" for a, b in POSITION_BINS]
    edges = [POSITION_BINS[0][0] - 1] + [b for _, b in POSITION_BINS]
    return pd.cut(pos, bins=edges, labels=labels)


_EPOCH = re.compile(r"Epoch: (\d+), validauc: ([0-9.]+).*?train loss: ([0-9.]+)")


def training_curve(train_log: Path) -> pd.DataFrame:
    """Validation AUC and training loss per epoch, parsed from a run's train.log."""
    rows = [
        {"epoch": int(m[1]), "valid_auc": float(m[2]), "train_loss": float(m[3])}
        for m in _EPOCH.finditer(Path(train_log).read_text())
    ]
    return pd.DataFrame(rows).drop_duplicates("epoch", keep="last")


def run_table(artifacts_dir: Path, models: list[str]) -> pd.DataFrame:
    """One row per finished (model, seed): epochs run, best epoch, seconds, s/epoch, params."""
    import json

    rows = []
    for m in models:
        for d in sorted(Path(artifacts_dir, m).glob("seed*")):
            f = d / "run_summary.json"
            if not f.exists():
                continue
            s = json.loads(f.read_text())
            curve = training_curve(d / "train.log") if (d / "train.log").exists() else pd.DataFrame()
            epochs = int(curve["epoch"].max()) if len(curve) else None
            rows.append(
                {
                    "model": m.upper(),
                    "seed": s["seed"],
                    "best_epoch": s["best_epoch"],
                    "epochs_run": epochs,
                    "valid_auc": s["valid_auc"],
                    "train_seconds": s["train_seconds"],
                    "seconds_per_epoch": round(s["train_seconds"] / epochs, 2) if epochs else None,
                    "n_params": s.get("n_params"),
                }
            )
    return pd.DataFrame(rows)
