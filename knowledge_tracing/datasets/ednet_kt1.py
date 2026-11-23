"""
EdNet-KT1 -> the answer-event schema that ``prepare_data.py`` reads.

EdNet (Choi et al., AIED 2020; CC BY-NC 4.0) is the interaction log of Santa,
a TOEIC English-preparation app. KT1 holds one CSV per user
(``KT1/u<id>.csv``: timestamp, solving_id, question_id, user_answer,
elapsed_time); ``contents/questions.csv`` gives each question's correct
answer, TOEIC part (1-7) and tags.

This is an external **benchmark** dataset. Its learners, items and KCs never
reach a Moodle learner model.

Mapping (documented choices, reported in the manifest):
  correct         = user_answer == questions.correct_answer
  item_id         = question_id
  kc_id           = first tag of questions.tags  ("tag<n>"); questions whose
                    tag is -1 (untagged) get one KC per TOEIC part
                    ("untagged_p<part>"). 46 % of questions carry several
                    tags; using only the first is a simplification.
  kc_dimension    = "part<p>", p = the most frequent TOEIC part among the
                    KC's questions (a tag can span parts; one dimension per KC)
  timestamp       = unix seconds (EdNet stores milliseconds)
  response_time_s = elapsed_time / 1000 (EdNet stores one time per bundle)

Sampling: the zip's user files are shuffled with ``--seed`` and read in that
order; a learner is eligible with at least ``--min-answers`` answers to known
questions. The first N eligible learners form the cohort of size N, so the
cohorts are nested (300 c 1000 c 2000 c 5000). Each history keeps its first
``--max-answers`` answers in time order, which is the same cut that
``prepare_data.MAX_SEQ_LEN`` applies.

The 1.2 GB zip is read in place; it is never extracted.

    .venv/bin/python knowledge_tracing/datasets/ednet_kt1.py \
        --kt1-zip data/external/ednet/EdNet-KT1.zip \
        --questions data/external/ednet/questions.csv \
        --sizes 300 1000 2000 5000
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import sys
import zipfile
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = PROJECT_ROOT / "data" / "external" / "ednet"
DEFAULT_SEED = 20240116
DEFAULT_SIZES = (300, 1000, 2000, 5000)
OUTPUT_COLUMNS = [
    "learner_id",
    "event_index",
    "item_id",
    "kc_id",
    "kc_dimension",
    "event_type",
    "timestamp",
    "response_time_s",
    "correct",
]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def load_questions(path: Path) -> pd.DataFrame:
    """question_id -> correct_answer, kc_id, kc_dimension, n_tags."""
    q = pd.read_csv(path, dtype=str)
    missing = {"question_id", "correct_answer", "part", "tags"} - set(q.columns)
    if missing:
        raise ValueError(f"questions.csv is missing columns: {sorted(missing)}")
    tags = q["tags"].fillna("-1").str.split(";")
    first = tags.str[0].str.strip()
    q["kc_id"] = ("tag" + first).where(first != "-1", "untagged_p" + q["part"])
    # A tag can occur in several TOEIC parts; prepare_data needs one dimension
    # per KC, so a KC takes its most frequent part (lowest part on a tie).
    modal = (
        q.groupby(["kc_id", "part"])
        .size()
        .rename("n")
        .reset_index()
        .sort_values(["kc_id", "n", "part"], ascending=[True, False, True])
        .drop_duplicates("kc_id")
        .set_index("kc_id")["part"]
    )
    q["kc_dimension"] = "part" + q["kc_id"].map(modal)
    q["part_differs_from_kc_dimension"] = q["kc_dimension"] != "part" + q["part"]
    q["n_tags"] = tags.str.len().where(first != "-1", 0)
    return q.set_index("question_id")[
        [
            "correct_answer",
            "kc_id",
            "kc_dimension",
            "n_tags",
            "part_differs_from_kc_dimension",
        ]
    ]


def convert_user(
    raw: pd.DataFrame, learner_id: str, questions: pd.DataFrame, max_answers: int
) -> tuple[pd.DataFrame, int]:
    """One user's KT1 rows -> schema rows. Returns (rows, unknown_question_rows)."""
    raw = raw.reset_index(drop=True)
    raw["_row"] = raw.index
    known = raw["question_id"].isin(questions.index)
    unknown = int((~known).sum())
    raw = raw[known]
    # Time order; solving_id and file order break ties inside a bundle.
    raw = raw.sort_values(["timestamp", "solving_id", "_row"], kind="mergesort")
    raw = raw.iloc[:max_answers]
    meta = questions.loc[raw["question_id"]]
    out = pd.DataFrame(
        {
            "learner_id": learner_id,
            "event_index": range(len(raw)),
            "item_id": raw["question_id"].to_numpy(),
            "kc_id": meta["kc_id"].to_numpy(),
            "kc_dimension": meta["kc_dimension"].to_numpy(),
            "event_type": "answer",
            "timestamp": (raw["timestamp"].astype("int64") // 1000).to_numpy(),
            "response_time_s": (raw["elapsed_time"].astype(float) / 1000.0).to_numpy(),
            "correct": (
                raw["user_answer"].astype(str).str.strip().str.lower().to_numpy()
                == meta["correct_answer"].astype(str).str.strip().str.lower().to_numpy()
            ).astype(int),
        }
    )
    return out[OUTPUT_COLUMNS], unknown


def sample_learners(
    zf: zipfile.ZipFile,
    questions: pd.DataFrame,
    n_max: int,
    seed: int,
    min_answers: int,
    max_answers: int,
    stats: dict,
):
    """Yield (learner_id, rows) for the first n_max eligible users in seeded order.

    Counters are written into ``stats`` as the generator runs.
    """
    members = sorted(
        n for n in zf.namelist() if n.startswith("KT1/u") and n.endswith(".csv")
    )
    random.Random(seed).shuffle(members)
    stats.update(
        user_files=len(members),
        files_read=0,
        ineligible_short=0,
        unknown_question_rows=0,
        eligible_taken=0,
    )
    taken = 0
    for name in members:
        if taken >= n_max:
            break
        stats["files_read"] += 1
        raw = pd.read_csv(
            io.BytesIO(zf.read(name)), dtype={"question_id": str, "user_answer": str}
        )
        learner_id = Path(name).stem  # "u246317"
        rows, unknown = convert_user(raw, learner_id, questions, max_answers)
        stats["unknown_question_rows"] += unknown
        if len(rows) < min_answers:
            stats["ineligible_short"] += 1
            continue
        taken += 1
        stats["eligible_taken"] = taken
        yield learner_id, rows


def build(
    kt1_zip: Path,
    questions_path: Path,
    out_dir: Path,
    sizes,
    seed: int,
    min_answers: int,
    max_answers: int,
    hash_sources: bool = True,
) -> dict:
    sizes = sorted(set(int(s) for s in sizes))
    questions = load_questions(questions_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    frames, stats = [], {}
    with zipfile.ZipFile(kt1_zip) as zf:
        for i, (_, rows) in enumerate(
            sample_learners(
                zf, questions, sizes[-1], seed, min_answers, max_answers, stats
            )
        ):
            rows.insert(0, "_rank", i)
            frames.append(rows)
            if (i + 1) % 500 == 0:
                print(f"  {i + 1:,} learners sampled", flush=True)
    if not frames:
        sys.exit("ERROR: no eligible learners found")
    events = pd.concat(frames, ignore_index=True)
    n_found = int(events["_rank"].max()) + 1
    if n_found < sizes[-1]:
        print(f"WARNING: only {n_found} eligible learners (asked for {sizes[-1]})")

    cohorts = {}
    for n in sizes:
        cohort = events[events["_rank"] < n].drop(columns="_rank")
        path = out_dir / f"kt1_events_{n}.csv"
        cohort.to_csv(path, index=False)
        item_meta = questions.loc[cohort["item_id"].unique()]
        item_tags = item_meta["n_tags"]
        cohorts[str(n)] = {
            "file": path.name,
            "learners": int(cohort["learner_id"].nunique()),
            "answers": int(len(cohort)),
            "items": int(cohort["item_id"].nunique()),
            "kcs": int(cohort["kc_id"].nunique()),
            "positive_rate": round(float(cohort["correct"].mean()), 4),
            "mean_seq_len": round(float(cohort.groupby("learner_id").size().mean()), 2),
            "items_multi_tag_share": round(float((item_tags > 1).mean()), 4),
            "items_untagged_share": round(float((item_tags == 0).mean()), 4),
            "items_part_differs_from_kc_dimension_share": round(
                float(item_meta["part_differs_from_kc_dimension"].mean()), 4
            ),
        }
        print(f"  wrote {path.name}: {cohorts[str(n)]}")

    manifest = {
        "dataset": "EdNet-KT1 (Choi et al., AIED 2020), CC BY-NC 4.0",
        "kt1_zip": str(kt1_zip),
        "questions": str(questions_path),
        "kt1_zip_sha256": sha256_file(kt1_zip) if hash_sources else None,
        "questions_sha256": sha256_file(questions_path) if hash_sources else None,
        "seed": seed,
        "min_answers": min_answers,
        "max_answers": max_answers,
        "kc_rule": "first tag of questions.tags; tag -1 -> untagged_p<part>",
        "kc_dimension_rule": "most frequent TOEIC part of the KC's questions",
        "nested_cohorts": True,
        "sampling": stats,
        "cohorts": cohorts,
    }
    (out_dir / "ednet_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--kt1-zip", type=Path, default=DEFAULT_DIR / "EdNet-KT1.zip")
    ap.add_argument("--questions", type=Path, default=DEFAULT_DIR / "questions.csv")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--sizes", nargs="+", type=int, default=list(DEFAULT_SIZES))
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--min-answers", type=int, default=20)
    ap.add_argument("--max-answers", type=int, default=200)
    args = ap.parse_args()
    manifest = build(
        args.kt1_zip,
        args.questions,
        args.output_dir,
        args.sizes,
        args.seed,
        args.min_answers,
        args.max_answers,
    )
    print(json.dumps(manifest["sampling"], indent=2))
    print(f"Manifest: {args.output_dir / 'ednet_manifest.json'}")


if __name__ == "__main__":
    main()
