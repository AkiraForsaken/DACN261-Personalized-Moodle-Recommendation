# api: the engine service

Moodle asks, the engine computes, Moodle imports. The service is the boundary
between the Moodle plugin `local_microlearning` and the Python engine:

```
Moodle (observer, ad hoc task, nightly task)
   │  POST refresh / run          X-API-Token
   ▼
engine service ── collector (read-only DB) → model stages → bundle
   │  GET bundle.zip
   ▼
Moodle imports it with the same checks as a CLI import
```

The engine never writes to Moodle, and Moodle's tables act as the cache: if
the engine is down, the block keeps showing the stored queue and refresh
requests wait until it is back.

```bash
set -a; . moodle-docker/api.env; set +a                # API_TOKEN (same value Moodle sends)
export MOODLE_DOCKER_DBPASS=...                        # the collector's read-only access
.venv/bin/python engine-api.py [--host 127.0.0.1] [--port 8010]
```

Runs in `.venv` (MariaDB driver, FastAPI, uvicorn); the model stages run in
`.venv-kt`, as with the worker. With Docker Desktop on WSL the Moodle
container reaches a service bound to `127.0.0.1` at
`http://host.docker.internal:8010` (allow a few seconds after start-up).

## Routes

Every route except `/health` needs the header `X-API-Token`. No interactive
docs, no CORS: Moodle's PHP is the only client.

| Route | Purpose |
| --- | --- |
| `GET /health` | liveness, course list, active jobs; no learner data |
| `POST /v1/courses/{c}/events` `{userid, type}` | a learner's list may be stale (`list_done`, `quiz_failed`, `new_learner`, `manual`): queue a refresh (202) |
| `POST /v1/courses/{c}/learners/{u}/refresh?wait=1` | recompute one learner (~1 s); 200 with the job and `result.bundle.bundle_id` |
| `POST /v1/courses/{c}/runs?wait=1` | rebuild the course (~1 min) |
| `GET /v1/jobs/{id}` | a job's state, timings, bundle, error |
| `GET /v1/courses/{c}/bundles/{id}.zip` | a finished bundle (410 once a newer run replaced it) |
| `GET /v1/courses/{c}/learners/{u}/profile` | profile, dimensions, skills, KCs with levels, quiz attempts (JSON) |
| `GET /v1/courses/{c}/learners/{u}/recommendations` | the queue and the failure branches (JSON) |
| `GET /v1/courses/{c}/cohort` | learners by population and phase, pending refreshes |

`?wait=1` blocks up to `ENGINE_API_WAIT_SECONDS` (120) and then answers 202
with the job id. A failed job answers 502 with its error.

## How it works

- **Jobs** (`jobs.py`): one runner thread per course, one job at a time.
  Refresh requests join the course's queued refresh job (several learners per
  run, as the worker did); full runs join a queued full run. A job holds the
  course's file lock, so the service and a polling worker never write the same
  workspace at once.
- **Work** is `moodle_sync.refresh.refresh_learners` / `full_run`, unchanged,
  with a `PublishTransport`: instead of pushing into Moodle it records the
  bundle. A bundle's id is the first 20 hex digits of the SHA-256 of its
  `manifest.json` (`bundles.py`), so an id names exactly one content.
- **Moodle** imports the downloaded zip with `importer::import_any`: every
  check (schema, course, checksums, users, course modules) applies as for a
  CLI import. If a learner update cannot apply (no snapshot yet, question
  mapping changed), the plugin asks for a full run instead.

## Moodle side (`local_microlearning` 0.3.0)

- Settings `engineurl`, `enginetoken`, `enginetimeout` (forced in the local
  `moodle/config.php` from the container's `API_TOKEN`).
- The observer still writes one pending row per learner in
  `local_microlearning_refresh` and now also queues the ad hoc task
  `refresh_learner`, so no page waits for the engine. If the engine is down
  the task fails and cron retries it; the request stays pending and the block
  keeps working from the stored queue.
- The scheduled task `full_sync` (03:00) rebuilds every course with an active
  snapshot.
- The privacy provider declares the engine as an external location.

## Tests and verification

`tests/test_api.py` covers authentication, refresh and full run with the real
model stages, the bundle zip, batching and failures. What was checked live
against a running Moodle: [docs/test_course_results.md](../docs/test_course_results.md).
