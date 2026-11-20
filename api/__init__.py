"""The engine's HTTP service: Moodle asks, the engine computes, Moodle imports.

    python engine-api.py            (= python -m api; runs in .venv)

Moodle's plugin sends events and requests and downloads finished bundles;
the engine never writes to Moodle (it keeps read-only database access for the
collector). Moodle's tables are a cache, so the block keeps working while the
engine is down. See api/README.md.
"""

API_VERSION = "1.0.0"
