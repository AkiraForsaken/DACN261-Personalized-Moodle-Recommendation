#!/usr/bin/env python3
"""
Thin CLI entry point for the knowledge engine.

    python knowledge-engine.py status   --course ID
    python knowledge-engine.py estimate --course ID [--as-of ...] [--estimator bkt]
    python knowledge-engine.py train    --course ID [--population simulated|moodle_collector]
    python knowledge-engine.py simulate

--course defaults to $MOODLE_COURSE_ID; everything is read from and written to
that course's workspace (courses/<site>/<course_id>/).

Same as `python -m knowledge_engine`. All logic lives in the
`knowledge_engine/` package (see knowledge_engine/README.md). `train`, and
`estimate` once a deep model is promoted, need the PyTorch environment
(.venv-kt).
"""

from knowledge_engine.__main__ import run

if __name__ == "__main__":
    run()
