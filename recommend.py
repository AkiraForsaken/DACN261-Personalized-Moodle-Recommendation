#!/usr/bin/env python3
"""
Thin CLI entry point for the recommender.

    python recommend.py generate --course ID
    python recommend.py evaluate --course ID [--cutoffs 6] [--horizon-days 7]

Same as `python -m recommender`. All logic lives in the `recommender/` package
(see recommender/README.md). The file is not called recommender.py so that it
cannot be mistaken for the package of that name.
"""

from recommender.__main__ import run

if __name__ == "__main__":
    run()
