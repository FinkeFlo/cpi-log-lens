# CPI Log Lens — Copilot Instructions

Self-hosted app for downloading, storing, and querying SAP Cloud Integration (CPI)
logs. FastAPI + DuckDB backend, Alpine.js (no build step) frontend.

## Running / building

There is no test suite, linter, or build step in this repo — validate changes by
running the app and exercising the affected endpoints/UI manually.

```bash
# Full stack via Docker (serves on http://localhost:8080)
docker compose up --build

# Mock mode — imports backend/mock/*.log instead of calling a real CPI tenant
MOCK=true docker compose up --build

# Backend only, without Docker (frontend is served from ../frontend by main.py)
cd backend
pip install -r requirements.txt
DB_PATH=../data/cpi_logs.duckdb LOGS_DIR=../data/logs \
  TENANTS_CONFIG=../config/tenants.jsonc FRONTEND_DIR=../frontend \
  uvicorn main:app --reload --port 8080
```

`docker-compose.yml` runs the image built from the root `Dockerfile`, with the
backend and frontend baked in (no `--reload`, non-root user); rebuild with
`docker compose up --build` after changes. For development use the override,
which bind-mounts `./backend` and `./frontend` and enables live reload:
`docker compose -f docker-compose.yml -f compose.dev.yaml up --build`.

Frontend has no bundler: `frontend/index.html` loads Tailwind, DaisyUI, Alpine.js
and Chart.js from CDNs plus a single local `app.js`. Edit `app.js`/`index.html`
directly and reload the browser.

## Architecture

- **`backend/main.py`** — FastAPI app, all HTTP/SSE routes, and the background
  fetch-job orchestration (`FetchJob` dataclass + `_run_fetch`). Only one fetch
  job can run at a time (`_active_job` module global); progress is broadcast to
  browser tabs via Server-Sent Events (`/api/fetch/stream`), with `/api/fetch/status`
  as a polling fallback for late subscribers.
- **`backend/db.py`** — all DuckDB access. `DuckDBConnection` wraps a single
  synchronous `duckdb` connection behind an `asyncio.Lock` (DuckDB permits only
  one writer/reader at a time), so every DB call goes through `db.run(fn, ...)`.
  Also owns the `LINE_RE` regex that parses CPI's `#`-delimited log line format
  and the schema (`logs`, `tenants`, `file_imports`, `fetch_runs` tables).
- **`backend/api.py`** — thin CPI REST client (OAuth2 client-credentials token,
  list/download log files via the CPI `LogFiles` OData endpoint).
- **Tenant credentials** can be seeded from `config/tenants.jsonc` (JSONC, gitignored) and are
  loaded into the `tenants` table at startup by `_load_tenants_from_json()` in
  `main.py`. They can also be added/edited at runtime via `/api/tenants*`
  endpoints from the Settings page — the JSON file is only read once at startup.
- **Incremental log import**: for each remote log file, `file_imports` tracks
  `lines`/`size` already imported. On re-fetch, only new lines/bytes beyond what
  was previously imported are parsed and inserted (`ON CONFLICT DO NOTHING` plus
  a `UNIQUE` constraint on the logs table also guards against duplicates).
- **`/api/query` and `/api/query/schema`** are a separate LLM/automation-friendly
  API layer on top of the same `query_logs()` used by the UI's `/api/logs` — they
  return a natural-language `summary` in addition to raw `items`. Keep both in
  sync if you change filter semantics in `db.query_logs`.
- **Frontend (`frontend/app.js`)** is a single global Alpine.js component
  (`App()`) with one flat state object; page routing is a simple `page` string
  synced to the URL hash (`_applyHash`), not a router library.

## Conventions

- Tenant IDs are always lowercase, no spaces (e.g. `dev`, `qas`, `prd`); the
  string `"all"` is a reserved sentinel meaning "no tenant filter" throughout
  the query/fetch APIs (`db.py`, `main.py`).
- `client_secret` is masked as `"••••••••"` in `GET /api/tenants` responses;
  `PUT /api/tenants/{id}` detects an all-`•` secret in the request body and
  keeps the existing stored secret instead of overwriting it.
- Log parsing (`db.parse_log_file`) transparently handles both plain-text and
  gzip log files by sniffing the magic bytes — don't assume `.gz` extension.
- New DB schema changes go in `db.SCHEMA` using `CREATE TABLE IF NOT EXISTS` /
  `CREATE INDEX IF NOT EXISTS` so they apply cleanly to existing `data/*.duckdb`
  files without a migration step (see `migrate_sqlite_to_duckdb.py` for the
  one-off SQLite→DuckDB migration path, not used in normal operation).
- `data/` and `config/tenants.jsonc` are gitignored — never commit real tenant
  credentials or database files.
