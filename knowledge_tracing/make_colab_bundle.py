#!/usr/bin/env python3
"""Pack the EdNet-KT1 benchmark for Google Colab into one zip.

The zip holds exactly what the Chapter 4 notebooks
(``knowledge_tracing/notebooks/1x_*.ipynb``: runner 13 and the CPU analysis notebooks) need, so Colab never
needs the 1.2 GB EdNet archive, git access or uncommitted files:

    code/   knowledge_tracing/*.py, datasets/, run_ednet_benchmark.sh,
            configs/model_configs.json (copied, read-only), and the four
            knowledge_engine modules hardening.py imports (bkt, config, schemas)
    data/   data/external/ednet/kt1_events_<N>.csv for the requested cohorts,
            ednet_manifest.json, questions.csv
    BUNDLE_MANIFEST.json   sha256 of every file, git HEAD and whether the
            working tree had uncommitted changes, so a Colab result can be
            traced to the exact code that produced it

Cohorts that are missing locally are built first with datasets/ednet_kt1.py
from data/external/ednet/EdNet-KT1.zip (same seed and filters, so the cohorts
stay nested: the first 5,000 learners of the 20,000 cohort are the 5,000 cohort).

Usage (project root):
    .venv-kt/bin/python knowledge_tracing/make_colab_bundle.py
    .venv-kt/bin/python knowledge_tracing/make_colab_bundle.py --sizes 300 1000 2000 5000
    .venv-kt/bin/python knowledge_tracing/make_colab_bundle.py --sizes 300 1000 2000 5000 10000 20000 50000

Output: data/colab/ednet_colab_bundle.zip (gitignored). Upload it to
Google Drive as MyDrive/ednet_kt1/ednet_colab_bundle.zip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EDNET_DIR = PROJECT_ROOT / "data" / "external" / "ednet"
DEFAULT_SIZES = [300, 1000, 2000, 5000, 10000, 20000, 50000]
DEFAULT_OUT = PROJECT_ROOT / "data" / "colab" / "ednet_colab_bundle.zip"

CODE_FILES = [
    "knowledge_tracing/__init__.py",
    "knowledge_tracing/kt_common.py",
    "knowledge_tracing/prepare_data.py",
    "knowledge_tracing/init_pykt_dataset.py",
    "knowledge_tracing/train.py",
    "knowledge_tracing/evaluate.py",
    "knowledge_tracing/inference.py",
    "knowledge_tracing/hardening.py",
    "knowledge_tracing/learning_curve.py",
    "knowledge_tracing/run_ednet_benchmark.sh",
    "knowledge_tracing/run_benchmark.sh",
    "knowledge_tracing/tune.py",
    "knowledge_tracing/analysis.py",
    "knowledge_tracing/eda.py",
    "knowledge_tracing/kt_results.py",
    "knowledge_tracing/report_utils.py",
    "knowledge_tracing/notebooks/10_ednet_data_preparation.ipynb",
    "knowledge_tracing/notebooks/11_ednet_eda.ipynb",
    "knowledge_tracing/notebooks/12_english_data_preparation_and_eda.ipynb",
    "knowledge_tracing/notebooks/13_kt_training_runner.ipynb",
    "knowledge_tracing/notebooks/14_kt_results_ednet.ipynb",
    "knowledge_tracing/notebooks/15_kt_results_english.ipynb",
    "knowledge_tracing/datasets/__init__.py",
    "knowledge_tracing/datasets/ednet_kt1.py",
    "knowledge_tracing/configs/model_configs.json",
    "knowledge_tracing/extra_model_configs.json",
    "knowledge_engine/__init__.py",
    "knowledge_engine/bkt.py",
    "knowledge_engine/config.py",
    "knowledge_engine/schemas.py",
    # English Learning Events (synthetic): raw log, the curated preparation and the
    # curated run's search and test metrics, read-only, for notebooks 12 and 15.
    "data/raw/english_learning_events.csv",
    "data/processed/split_assignments.csv",
    "data/processed/question_map.csv",
    "data/processed/concept_map.csv",
    "data/processed/pykt_english/train_valid_sequences.csv",
    "data/processed/pykt_english/test_sequences.csv",
    "knowledge_tracing/artifacts/tuning/tuning_results.csv",
    "knowledge_tracing/artifacts/evaluation/test_comparison.csv",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def ensure_cohorts(sizes: list[int]) -> None:
    missing = [n for n in sizes if not (EDNET_DIR / f"kt1_events_{n}.csv").exists()]
    if not missing:
        return
    if not (EDNET_DIR / "EdNet-KT1.zip").exists():
        sys.exit(f"ERROR: cohorts {missing} are missing and {EDNET_DIR}/EdNet-KT1.zip is not there")
    # Rebuild every requested size in one pass: the sample is deterministic and
    # nested, so the existing files are rewritten byte-identically (checked below).
    before = {n: sha256(EDNET_DIR / f"kt1_events_{n}.csv") for n in sizes if n not in missing}
    print(f"Building EdNet cohorts {sorted(set(sizes))} (missing: {missing}) ...", flush=True)
    subprocess.run([sys.executable, str(PROJECT_ROOT / "knowledge_tracing/datasets/ednet_kt1.py"),
                    "--sizes", *map(str, sorted(set(sizes)))], cwd=PROJECT_ROOT, check=True)
    changed = [n for n, h in before.items() if sha256(EDNET_DIR / f"kt1_events_{n}.csv") != h]
    if changed:
        sys.exit(f"ERROR: rebuilding changed existing cohorts {changed}; the sample is not reproducible")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", nargs="+", type=int, default=DEFAULT_SIZES)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    ensure_cohorts(args.sizes)
    data_files = [f"data/external/ednet/kt1_events_{n}.csv" for n in sorted(set(args.sizes))]
    data_files += ["data/external/ednet/ednet_manifest.json", "data/external/ednet/questions.csv"]

    files = CODE_FILES + data_files
    for rel in files:
        if not (PROJECT_ROOT / rel).is_file():
            sys.exit(f"ERROR: {rel} not found")

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": git("rev-parse", "HEAD"),
        "uncommitted_changes": git("status", "--porcelain", "--", "knowledge_tracing",
                                   "knowledge_engine").splitlines(),
        "sizes": sorted(set(args.sizes)),
        "sha256": {rel: sha256(PROJECT_ROOT / rel) for rel in files},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel in files:
            zf.write(PROJECT_ROOT / rel, rel)
        zf.writestr("BUNDLE_MANIFEST.json", json.dumps(manifest, indent=2))
    mb = args.out.stat().st_size / 1e6
    print(f"Wrote {args.out} ({mb:.1f} MB): {len(CODE_FILES)} code files, cohorts {manifest['sizes']}")
    if manifest["uncommitted_changes"]:
        print(f"Note: bundled code includes {len(manifest['uncommitted_changes'])} uncommitted "
              "change(s); recorded in BUNDLE_MANIFEST.json")
    print("Upload it to Google Drive as MyDrive/ednet_kt1/ednet_colab_bundle.zip")


if __name__ == "__main__":
    main()
