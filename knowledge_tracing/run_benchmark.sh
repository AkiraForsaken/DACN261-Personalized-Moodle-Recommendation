#!/usr/bin/env bash
# Knowledge-tracing benchmark runner for every public dataset of Chapter 4.
#
#   DATASET=ednet_kt1     every nested EdNet-KT1 cohort in $SIZES
#                         (data/external/ednet/kt1_events_<N>.csv -> $BENCH_DIR/ednet_kt1_<N>/kt),
#                         then the learning curve
#   DATASET=english_bench the synthetic English Learning Events log
#                         (data/raw/english_learning_events.csv -> $BENCH_DIR/english_kt/kt),
#                         same split as the curated english_kt preparation
#
# Per workspace: prepare -> (optional search: TUNE_MODELS) -> train (DKT/SAKT/AKT/DKVMN/SAINT
# x 3 seeds) -> evaluate on the test fold -> hardening (BKT, logistic regression,
# bootstrap CIs, paired differences).
#
#   DATASET=english_bench TUNE_MODELS="dkvmn saint" knowledge_tracing/run_benchmark.sh
#   DATASET=ednet_kt1 TRAIN_MODELS="dkvmn saint" EVAL_FROM_DUMPS="dkt sakt akt" knowledge_tracing/run_benchmark.sh
#   SIZES="300" EPOCHS=2 SEEDS=42 BENCH_DIR=/tmp/smoke knowledge_tracing/run_benchmark.sh   # smoke
#
# Writes only under $BENCH_DIR; never touches knowledge_tracing/configs/ or
# knowledge_tracing/artifacts/ (the curated english_kt run stays read-only).
#
# Resumable (RESUME=1, the default): a prepared workspace is not re-prepared, a
# (model, seed) run that finished with the same configuration is reused
# (train.py --skip-existing), and evaluation is skipped when it is newer than
# every finished run and covered the same models. RESUME=0 redoes everything.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv-kt/bin/python}"
DATASET="${DATASET:-ednet_kt1}"
SIZES="${SIZES:-300 1000 2000 5000 10000 20000}"
SEEDS="${SEEDS:-42 43 44}"
# TRAIN_MODELS are trained; EVAL_MODELS are evaluated and hardened (inference only
# for models trained earlier). MODELS, if set, is the default for both.
MODELS="${MODELS:-dkt sakt akt dkvmn saint}"
TRAIN_MODELS="${TRAIN_MODELS:-$MODELS}"
EVAL_MODELS="${EVAL_MODELS:-$MODELS}"
TUNE_MODELS="${TUNE_MODELS:-}"
# EVAL_FROM_DUMPS: models scored from their saved prediction dumps because their
# checkpoints were not kept (EdNet DKT/SAKT/AKT from the first Colab run).
EVAL_FROM_DUMPS="${EVAL_FROM_DUMPS:-}"
RESUME="${RESUME:-1}"
BENCH_DIR="${BENCH_DIR:-$PWD/data/benchmarks}"
EPOCH_ARG=()
[[ -n "${EPOCHS:-}" ]] && EPOCH_ARG=(--epochs "$EPOCHS")
SKIP_ARG=()
[[ "$RESUME" == "1" ]] && SKIP_ARG=(--skip-existing)

stamp() { date '+%H:%M:%S'; }

run_workspace() {  # $1 = input CSV, $2 = workspace kt/ dir, $3 = label
  local INPUT="$1" LABEL="$3"
  export KT_COURSE_DIR="$2"
  local EVAL="$KT_COURSE_DIR/artifacts/evaluation"
  echo "=== $(stamp) ${LABEL} -> ${KT_COURSE_DIR}"
  [[ -f "$INPUT" ]] || { echo "ERROR: $INPUT missing"; exit 1; }

  if [[ "$RESUME" == "1" && -f "$KT_COURSE_DIR/data_config.json" \
        && "$KT_COURSE_DIR/data_config.json" -nt "$INPUT" ]]; then
    echo "--- $(stamp) prepare: already done, reused"
  else
    "$PY" knowledge_tracing/prepare_data.py --dataset-name "$DATASET" --input "$INPUT"
    "$PY" knowledge_tracing/init_pykt_dataset.py --dataset-name "$DATASET"
  fi

  if [[ -n "$TUNE_MODELS" ]]; then
    # shellcheck disable=SC2086
    "$PY" knowledge_tracing/tune.py --dataset-name "$DATASET" --models $TUNE_MODELS "${SKIP_ARG[@]}"
  fi

  # shellcheck disable=SC2086
  "$PY" knowledge_tracing/train.py --dataset-name "$DATASET" \
      --models $TRAIN_MODELS --seeds $SEEDS "${EPOCH_ARG[@]}" "${SKIP_ARG[@]}"

  if [[ "$RESUME" == "1" && -f "$EVAL/hardening_summary.json" \
        && "$(cat "$EVAL/evaluated_models.txt" 2>/dev/null)" == "$EVAL_MODELS" \
        && -z "$(find "$KT_COURSE_DIR/artifacts" -path '*/tuning' -prune -o -name run_summary.json -newer "$EVAL/hardening_summary.json" -print)" ]]; then
    echo "--- $(stamp) evaluate + hardening: up to date, skipped"
  else
    # shellcheck disable=SC2086
    "$PY" knowledge_tracing/evaluate.py --dataset-name "$DATASET" --models $EVAL_MODELS \
        --from-dumps $EVAL_FROM_DUMPS
    # shellcheck disable=SC2086
    "$PY" knowledge_tracing/hardening.py --dataset-name "$DATASET" --models $EVAL_MODELS
    echo "$EVAL_MODELS" > "$EVAL/evaluated_models.txt"
  fi
  echo "=== $(stamp) ${LABEL} done"
}

case "$DATASET" in
  ednet_kt1)
    for N in $SIZES; do
      run_workspace "data/external/ednet/kt1_events_${N}.csv" \
                    "$BENCH_DIR/ednet_kt1_${N}/kt" "EdNet-KT1 cohort ${N}"
    done
    unset KT_COURSE_DIR
    "$PY" knowledge_tracing/learning_curve.py --bench-dir "$BENCH_DIR"
    ;;
  english_bench)
    # ENGLISH_WS: a second workspace for a sensitivity run, e.g. ENGLISH_WS=english_kt_searched
    # with KT_MODEL_CONFIG_OVERRIDES naming each model's best searched configuration.
    run_workspace "data/raw/english_learning_events.csv" \
                  "$BENCH_DIR/${ENGLISH_WS:-english_kt}/kt" "English Learning Events (synthetic)"
    ;;
  *)
    echo "ERROR: DATASET must be ednet_kt1 or english_bench"; exit 2 ;;
esac
