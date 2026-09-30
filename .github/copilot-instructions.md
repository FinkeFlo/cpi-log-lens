# CPI Log Lens — contributor notes

Self-hosted app that fetches SAP Cloud Integration (CPI) log files, stores them in DuckDB and
makes them searchable. FastAPI backend, Alpine.js frontend without a build step.
See CONTRIBUTING.md for setup and commit conventions, and docs/adr/ for design decisions.

## Running

```bash
docker compose up -d --build                                          # like production
docker compose -f docker-compose.yml -f compose.dev.yaml up --build   # live reload
```

`MOCK=true`, or the "Try demo data" button (tenant `demo` with `demo://` URLs), imports
`backend/mock/trace_sample.log` (synthetic, regenerate with `backend/mock/generate_sample.py`)
instead of calling CPI. CI (`.github/workflows/ci.yml`) runs ruff (lint + format), mypy, pytest,
pip-audit and an image smoke test. Dependencies: `backend/pyproject.toml`, locked in
`backend/uv.lock` (`uv sync`, `uv lock`).

Tests (`backend/tests/`, `uv run pytest`): API tests through httpx `ASGITransport` with a fresh DuckDB
file per test; CPI calls go to the in-process fake in `tests/support.py`. Change behaviour together
with its tests; strict `xfail` markers pin known bugs and must be removed with the fix.

## Architecture

- `backend/config.py` — all settings (`Settings`, pydantic-settings; env var = upper-case field
  name). Read them via `get_settings()`, never with `os.getenv`.
- `backend/main.py` — FastAPI app: `lifespan` (open DB, seed tenants, start loops; on shutdown
  cancel them, checkpoint and close the DB), routes, request models (validated with Pydantic),
  middleware (trusted hosts, cross-origin write guard, request log), the fetch job (`FetchJob`,
  `_run_fetch`; one job at a time, progress via SSE on `/api/fetch/stream`), scheduler and
  retention loops, tenant seeding from `TENANTS_CONFIG`, `/healthz`, `/readyz`, event-loop watchdog.
- `backend/db.py` — all DuckDB access. `DuckDBConnection.open()` connects and migrates; the
  wrapper has one writer (`run()`, single writer
  thread, `WRITE_LOCK_TIMEOUT_S`) and a bounded pool of read cursors (`read()`, pool and query
  timeouts, `cursor.interrupt()`; busy → `DBBusyError` → HTTP 503). Schema and ad-hoc migrations,
  the streaming parser `iter_log_batches` and `import_log_file`, queries and a short stats cache.
- `backend/api.py` — CPI client: OAuth token, file list, streaming download to gzip on disk, retry
  with backoff.
- `backend/logging_config.py` — stdout logging, `LOG_FORMAT=text|json`.
- `frontend/index.html` + `frontend/app.js` — single Alpine component `App()`, hash routing;
  libraries vendored in `frontend/vendor/` (`scripts/vendor-frontend.sh`).
- `Dockerfile` (repo root) — multi-stage, non-root, `HEALTHCHECK`, no `--reload`.

## Conventions

- Tenant IDs: lowercase letters, digits, `-`, `_`; `"all"` is reserved as "every tenant".
- Duplicate protection comes from `file_imports.lines` only (no UNIQUE constraint, see ADR 2);
  keep rows and offset in the same transaction.
- Never do CPU-heavy or blocking work on the event loop; use the DB threads or `asyncio.to_thread`.
- Never read DuckDB results with `fetchone()` and leave them open; use the `_fetch*` helpers.
- `/api/query` and `/api/query/schema` form a contract for external tools; keep them in sync with
  `db.query_logs` filter semantics.
- UI and API texts are English. Don't commit credentials, service keys or real log data.
