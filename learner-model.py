#!/usr/bin/env python3
"""
Thin CLI entry point for the learner model.

    python learner-model.py --course ID [--as-of ...]

Same as `python -m learner_model.builder`. All logic lives in the
`learner_model/` package (see learner_model/README.md). Run the knowledge
engine first so the profiles include knowledge states.
"""

from learner_model.builder import main

if __name__ == "__main__":
    main()
