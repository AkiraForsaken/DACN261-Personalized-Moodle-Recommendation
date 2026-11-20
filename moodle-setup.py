#!/usr/bin/env python3
"""
Thin CLI entry point for setting up the local Moodle test instance.

    python moodle-setup.py build-course [--rebuild]
    python moodle-setup.py simulate-learners [--learners N] [--seed S]
    python moodle-setup.py remove-simulated
    python moodle-setup.py check-content

Same as `python -m moodle_setup`. All logic lives in the `moodle_setup/`
package (see moodle_setup/README.md). These commands write to Moodle: back up
the database first.
"""

from moodle_setup.__main__ import main

if __name__ == "__main__":
    main()
