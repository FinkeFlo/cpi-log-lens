# CPI Log Lens — contributor notes

Self-hosted app that fetches SAP Cloud Integration (CPI) log files, stores them in DuckDB and
makes them searchable. FastAPI backend, Alpine.js frontend without a build step.
See CONTRIBUTING.md for setup and commit conventions, and docs/adr/ for design decisions.

## Running

```bash
docker compose up -d --build                                          # like production
docker compose -f docker-compose.yml -f compose.dev.yaml up --build   # live reload
```

`MOCK=true`, or the "Try demo data" button (tenant `demo` with `demo://` URLs), fetches
`backend/mock/trace_sample.log` (synthetic, regenerate with `backend/mock/generate_sample.py`) from
the in-process fake CPI server instead of SAP, through the same code path as a real tenant. CI
(`.github/workflows/ci.yml`) runs ruff (lint + format), mypy, pytest, pip-audit and an image smoke
test. Dependencies: `backend/pyproject.toml`, locked in `backend/uv.lock` (`uv sync`, `uv lock`).

Tests (`backend/tests/`, `uv run pytest`): API tests through httpx `ASGITransport` with a fresh
DuckDB file per test; CPI calls go to the in-process fake (`app/cpi/fake.py`, fixture `fake_cpi`).
Change behaviour together with its tests; strict `xfail` markers pin known bugs and must be removed
with the fix.

## Architecture

The backend is the `app` package in `backend/` (`uvicorn app.main:app`). Dependencies point one way:
`api` → `services` → `repositories` / `cpi` / `parsing`. `parsing` and `cpi` know nothing about the
database; `repositories` know nothing about FastAPI or HTTP.

- `app/main.py` — `create_app()`: error handlers, middleware, one router per resource, the UI mount.
- `app/lifespan.py` — start: open the database (`app.state.db`), seed tenants, start the background
  loops and the watchdog; stop: cancel them and a running fetch, checkpoint and close the database.
- `app/config.py` — all settings (`Settings`, pydantic-settings; env var = upper-case field name).
  Read them via `get_settings()`, never with `os.getenv`.
- `app/api/` — routers (`health`, `tenants`, `fetch` incl. SSE and demo, `schedules`, `logs`, `query`,
  `stats`, `admin`), request models in `schemas.py`, `middleware.py` (trusted hosts, cross-origin
  write guard, request log), `deps.py` (`DbDep`: the database for a request).
- `app/services/` — `fetch` (`FetchService` in `app.state.fetch`: one job at a time behind a lock,
  progress events for SSE, every run recorded in `fetch_runs`), `importer`
  (parse a file, insert rows past the stored offset), `scheduler` (`ScheduleService`: schedules and
  retention as APScheduler 3 jobs, reloaded after every schedule change),
  `tenants` (seeding from `TENANTS_CONFIG`, demo tenant), `stats` (cached statistics), `query`.
- `app/repositories/` — `database.py` (`Database`: one writer via `run()` in a single writer thread,
  a bounded pool of read cursors via `read()` with pool and query timeouts and
  `cursor.interrupt()`; busy → `DBBusyError` → HTTP 503; clean `close()`), one module per table
  group with SQL only.
- `app/migrations/` — numbered schema migrations (`vNNNN_name.sql|py`), applied by
  `Database.open()` and recorded in `schema_version`; a newer database stops the start.
- `app/storage.py` — storage version checks, the opt-in conversion to ZSTD-compressed log texts
  (`DB_STORAGE_UPGRADE`, `python -m app.storage`) and `backup_to()` for `POST /api/db/backup`.
- `app/parsing/cpi_log.py` — streaming parser `iter_log_batches` (pure, fully typed).
- `app/cpi/` — `CpiClient` (OAuth token, file list, streaming download to gzip on disk, retry with
  backoff; injectable httpx transport), `fake.py` (in-process fake CPI API with the bundled sample,
  used for the demo tenant, MOCK mode and tests), `client_for(tenant)` picks real or fake.
- `app/tasks.py` (background tasks kept referenced), `app/watchdog.py` (heartbeat and the thread
  that exits a hung process), `app/errors.py`, `app/logging_config.py` (`LOG_FORMAT=text|json`).
- `frontend/index.html` + `frontend/js/` — native ES modules, no build step: `main.js` registers the
  Alpine stores (`stores/`: route, tenants, fetch job, toast) and one component per page (`pages/`);
  `api.js` is the only place that calls the API; pages talk through window events (`events.js`);
  libraries vendored in `frontend/vendor/` (`scripts/vendor-frontend.sh`).
- `Dockerfile` (repo root) — multi-stage, non-root, `HEALTHCHECK`, no `--reload`.

## Conventions

- Tenant IDs: lowercase letters, digits, `-`, `_`; `"all"` is reserved as "every tenant".
- Schema changes only as a new migration file; never edit a released one.
- Duplicate protection comes from `file_imports.lines` only (no UNIQUE constraint, see ADR 2);
  keep rows and offset in the same transaction.
- Never do CPU-heavy or blocking work on the event loop; use the DB threads or `asyncio.to_thread`.
- Never read DuckDB results with `fetchone()` and leave them open; use `Database.fetch_*`.
- `/api/query` and `/api/query/schema` form a contract for external tools; keep them in sync with
  the filter semantics of `repositories/logs.query_logs`.
- Errors are HTTP status codes with `{"detail": …}` (`HTTPException` or a handler in
  `app/errors.py`); never 200 with an error flag.
- UI and API texts are English. Don't commit credentials, service keys or real log data.
