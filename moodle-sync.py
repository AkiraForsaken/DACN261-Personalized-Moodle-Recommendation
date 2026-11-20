#!/usr/bin/env python3
"""
Thin CLI entry point for publishing to Moodle.

    python moodle-sync.py bundle  --course ID          build the import bundle only
    python moodle-sync.py push    --course ID          build it and import it into Moodle
    python moodle-sync.py worker  --course ID [--once] answer Moodle's per-learner refresh requests
    python moodle-sync.py refresh --course ID --learners 7,12   refresh named learners by hand

Same as `python -m moodle_sync`. All logic lives in the `moodle_sync/` package
(see moodle_sync/README.md). `push`, `worker` and `refresh` need the Moodle
containers running; `worker` and `refresh` run in .venv (MariaDB driver) and
call .venv-kt for the model stages.
"""

from moodle_sync.__main__ import run

if __name__ == "__main__":
    run()
