#!/usr/bin/env python3
"""
Thin CLI entry point for the Moodle collector.

    python moodle-collector.py [--course ID] [--learners 7,12] [--output-dir DIR]

All logic lives in the `collector/` package (see collector/README.md).
Database settings are MOODLE_* environment variables (see collector/config.py). Output goes to the course workspace
(courses/<site>/<course_id>/data) unless --output-dir is given.
"""

from collector import main

if __name__ == "__main__":
    main()
