#!/usr/bin/env bash
# EdNet-KT1 benchmark: kept for the Colab notebooks and older instructions.
# The runner is knowledge_tracing/run_benchmark.sh (DATASET=ednet_kt1); see its
# header for SIZES, SEEDS, TRAIN_MODELS, EVAL_MODELS, EPOCHS, RESUME, BENCH_DIR.
#
#   knowledge_tracing/run_ednet_benchmark.sh                              # all cohorts, 5 models
#   SIZES="300" EPOCHS=2 SEEDS=42 knowledge_tracing/run_ednet_benchmark.sh   # smoke
#   TRAIN_MODELS="dkvmn saint" knowledge_tracing/run_ednet_benchmark.sh   # add models; old runs untouched
set -euo pipefail
DATASET=ednet_kt1 exec bash "$(dirname "$0")/run_benchmark.sh" "$@"
