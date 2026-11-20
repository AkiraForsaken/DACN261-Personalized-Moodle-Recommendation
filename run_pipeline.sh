#!/usr/bin/env bash
# Moodle -> learner profiles -> recommendations -> Moodle, for one course.
#
#   ./run_pipeline.sh --course 10              full run, including the import into Moodle
#   ./run_pipeline.sh --course 10 --no-push    stop after building the bundle
#
# The course id can also come from MOODLE_COURSE_ID. Everything is read from and
# written to the course's workspace, courses/<site>/<course_id>/ (see
# course_workspace). Database settings: MOODLE_DOCKER_DBPASS etc., see
# collector/config.py.
#
# Two virtualenvs are used: .venv has the MariaDB driver (collector), .venv-kt
# has PyTorch/pyKT (needed by `estimate` once a deep model is promoted).
# Training is not part of this script: run `python knowledge-engine.py train
# --course <id>` deliberately, when `status` says the data is ready.
set -euo pipefail
cd "$(dirname "$0")"

COLLECT_PY="${COLLECT_PY:-.venv/bin/python}"
MODEL_PY="${MODEL_PY:-.venv-kt/bin/python}"
SYNC_COMMAND="push"
COURSE="${MOODLE_COURSE_ID:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-push) SYNC_COMMAND="bundle"; shift ;;
    --course) COURSE="$2"; shift 2 ;;
    --course=*) COURSE="${1#*=}"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$COURSE" ]]; then
  echo "Which course? Pass --course <id> or set MOODLE_COURSE_ID." >&2
  exit 2
fi
COURSE_ARGS=(--course "$COURSE")

echo "== 1/5 collector";        "$COLLECT_PY" moodle-collector.py "${COURSE_ARGS[@]}"
echo "== 2/5 knowledge states"; "$MODEL_PY" knowledge-engine.py estimate "${COURSE_ARGS[@]}"
echo "== 3/5 learner model";    "$MODEL_PY" learner-model.py "${COURSE_ARGS[@]}"
echo "== 4/5 recommendations";  "$MODEL_PY" recommend.py generate "${COURSE_ARGS[@]}"
echo "== 5/5 moodle ($SYNC_COMMAND)"; "$MODEL_PY" moodle-sync.py "$SYNC_COMMAND" "${COURSE_ARGS[@]}"
