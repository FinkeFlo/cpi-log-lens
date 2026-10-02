# CPI Log Lens

<img src="frontend/logo.svg" width="64" align="right" />

Self-hosted tool to fetch, store and search the log files of SAP Cloud Integration (CPI) tenants.
It runs on your own machine, keeps everything in a local DuckDB database and works offline once
the logs are fetched.

- **Fetch** trace and HTTP log files from any number of CPI tenants (OAuth2 client credentials),
  manually or on a schedule. Re-fetches only import what is new.
- **Browse** entries by tenant, level, IFlow, text and time range; open any entry to see the full
  message and the original log line.
- **Stats**: level distribution, IFlows with the most errors, errors per hour.
- **LLM / automation API** to query logs from scripts or AI assistants.

Not affiliated with or endorsed by SAP SE.

## Quick start

With Docker (no clone, no build):

```bash
docker run -d --name cpi-log-lens \
  -p 127.0.0.1:8080:8080 \
  -v cpi-log-lens-data:/data \
  --restart unless-stopped --stop-timeout 60 \
  ghcr.io/finkeflo/cpi-log-lens:latest
```

Or from source with Docker Compose:

```bash
git clone https://github.com/FinkeFlo/cpi-log-lens
cd cpi-log-lens
docker compose up -d
```

Open <http://localhost:8080>. On first start you can **connect a CPI tenant** or **try the demo
data** (bundled synthetic logs, no credentials needed).

## Connecting SAP Cloud Integration

The app reads log files through the Cloud Integration OData API (`/api/v1/LogFiles`). You need a
service key of a *Process Integration Runtime* service instance (plan `api`) whose roles allow
reading log files; see the SAP documentation of the LogFiles API for the required role.

In **Settings → Add tenant**, paste the service key JSON: API URL, OAuth URL, client ID and secret
are filled in automatically. Give the tenant a short ID (lowercase, e.g. `prd`) and a display name.
**Test connection** checks the details before you save them: it requests a token and lists the
trace log files, so it also finds a service key without the role to read log files, and it says in
plain words what to fix (unknown host, rejected client ID or secret, missing role, …).

Deleting a tenant removes it from scheduled fetches. Its imported entries and downloaded files are
kept unless you choose to delete them too (`DELETE /api/tenants/<id>?purge=true`); kept data stays
consistent, so a tenant added again with the same ID continues where it stopped.

Alternatively, seed tenants from `config/tenants.jsonc` (see
[`config/tenants.jsonc.example`](config/tenants.jsonc.example)). By default the file only adds
tenants that don't exist yet, so changes made in the UI stay; set `TENANTS_SEED_MODE=sync` to make
the file the source of truth.

## Configuration

Everything is optional. With Compose, put variables in a `.env` file next to `docker-compose.yml`
(see [`.env.example`](.env.example)); with `docker run`, pass them with `-e`. Empty values count as
unset; an invalid value stops the app at start with a message naming the variable.

| Variable | Default | Description |
|---|---|---|
| `BIND_ADDRESS` | `127.0.0.1` | *(Compose)* Host interface the port is published on. |
| `APP_PORT` | `8080` | *(Compose)* Host port. |
| `APP_MEM_LIMIT` | `4g` | *(Compose)* Memory limit of the container. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host names the app answers to; add the name you use to open it. `*` disables the check (behind a reverse proxy). |
| `CORS_ORIGINS` | *(none)* | Extra browser origins allowed to call the API. The UI itself needs none. |
| `MOCK` | `false` | Import the bundled sample logs for every tenant instead of calling CPI. |
| `FETCH_CONCURRENCY` | `4` | Parallel file downloads per tenant and log type. |
| `RETENTION_DAYS` | `0` | Delete entries older than N days automatically (`0` = keep everything). |
| `RETENTION_CHECK_HOURS` | `24` | How often the retention job runs. |
| `TENANTS_CONFIG` | `/config/tenants.jsonc` | Tenant seed file (Compose mounts `./config`). |
| `TENANTS_SEED_MODE` | `create` | `create`: only add missing tenants. `sync`: the file overwrites stored tenants. |
| `DB_PATH` | `/data/cpi_logs.duckdb` | DuckDB database file. |
| `LOGS_DIR` | `/data/logs` | Downloaded log files (gzip). |
| `BACKUP_DIR` | `backups` next to the database | Where `POST /api/db/backup` writes backups. |
| `DB_STORAGE_UPGRADE` | `false` | On start, convert an existing database to the compressed storage format (see *Storage format*). |
| `FRONTEND_DIR` | `/app/frontend` | Directory of the web UI; the API is served alone if it is missing. |
| `DUCKDB_MEMORY_LIMIT` | `1.5GB` | Memory DuckDB may use. Keep well below the container limit. |
| `DUCKDB_THREADS` | `4` | Threads DuckDB may use. |
| `DUCKDB_TEMP_DIR` | *(next to the DB)* | Spill directory for large queries. |
| `DUCKDB_CHECKPOINT_THRESHOLD` | `512MB` | WAL size that triggers a checkpoint. |
| `QUERY_TIMEOUT_S` | `30` | Read queries running longer are cancelled (HTTP 503). |
| `POOL_ACQUIRE_TIMEOUT_S` | `10` | Max. wait for a free database connection before answering 503. |
| `WRITE_LOCK_TIMEOUT_S` | `120` | Max. wait of a write for the database writer. |
| `STATS_CACHE_SECONDS` | `30` | How long the Stats page result is cached. |
| `WATCHDOG_STALL_SECONDS` | `120` | Restart the app if its event loop is blocked this long (`0` = off). |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`. |
| `LOG_FORMAT` | `text` | `text` or `json` (one object per line). |

## Security

The app has **no login**. It is meant to run on your own machine and only listens on `127.0.0.1`
by default. Requests to other host names and write requests from other web pages are refused.

To make it reachable from other machines, set `BIND_ADDRESS=0.0.0.0`, add the host name to
`ALLOWED_HOSTS`, and put an authenticating reverse proxy in front of it (for example Caddy with
basic auth, or oauth2-proxy).

Tenant client secrets are stored in the database file: protect the data volume and its backups
accordingly. Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Operations

**Data.** Everything lives in `/data` (Compose: `./data`): the database `cpi_logs.duckdb` (plus a
`.wal` file while running) and the downloaded log files under `logs/`.

**Backup.** While the app runs, `POST /api/db/backup` writes a consistent copy of the database to
`/data/backups/cpi_logs-<UTC time>.duckdb` (Compose: `./data/backups`; set `BACKUP_DIR` to use another
directory, e.g. a mounted backup volume) and answers with its path and size; `GET /api/db/backups`
lists the files. Imports wait while the copy is written, so a backup is refused while a fetch runs.

```bash
curl -X POST http://localhost:8080/api/db/backup
```

Without the app, stop the container and copy the database file together with its `.wal` file, if
any. Don't copy the file of a running app: recent changes may only be in the WAL.

**Restore.** Stop the app, replace `cpi_logs.duckdb` with the backup file, delete a
`cpi_logs.duckdb.wal` if there is one, and start the app again. Backups contain the tenant secrets:
protect them like the database.

**Upgrading.** Pull the new image (`docker compose pull && docker compose up -d`, or rebuild from
source). On start the app applies pending schema migrations (logged as `migration NNNN …`); the
current version is shown in `GET /api/db/info` (`schema_version`). Back up the database before
upgrading across releases whose changelog mentions a storage change. Going back to an older app
version after a schema migration is not supported: the older app refuses to start with a message
("written by a newer app version"); restore the backup taken before the upgrade instead.

**Storage format.** New databases store the log texts ZSTD-compressed (DuckDB storage version
v1.5.0), which makes them about 2.5–4 times smaller than the previous format; text searches over
all entries take up to about a third longer. Databases created by earlier versions keep their
format until you convert them, once, with the app stopped:

```bash
docker compose stop
docker compose run --rm app python -m app.storage   # or start once with DB_STORAGE_UPGRADE=true
docker compose start
```

The conversion copies every table into a new file (needs free disk space of about the current
file size, and one to a few minutes for millions of entries), checks the copy, and swaps the files;
the old file is kept as `cpi_logs.duckdb.bak-<UTC time>`. Delete it once the app works with the new
file. **The conversion is one-way:** the new file can only be opened with DuckDB 1.5 or newer (all
releases of this app use DuckDB 1.5), not with older DuckDB tools. To go back, stop the app and
restore the `.bak` file.

**Resources.** Plan for 2 CPU cores and 2–4 GB RAM for a database of about 10 GB. Deleting old
entries (Settings or `RETENTION_DAYS`) keeps the database from growing without limit. The file does
not shrink after deletions, but the space is reused.

**Health.** `GET /healthz` reports liveness and the version, `GET /readyz` checks the database. The
image's `HEALTHCHECK` uses `/healthz`.

**Stopping.** On `docker stop` the app cancels a running fetch, lets the current database write
finish (up to 5 s), writes all pending changes from the WAL into the database file and closes it.
Allow enough stop time (Compose: `stop_grace_period: 60s`; `docker run`: `--stop-timeout 60`).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `400 Invalid host header` | You opened the app by a name that is not in `ALLOWED_HOSTS`. |
| `403 cross-origin request refused` | A write request came from another web page; use the app's own UI or a script without an `Origin` header. |
| Test connection: client ID or secret rejected (HTTP 401) | Copy client ID and secret from the service key again. |
| Test connection: credentials work, but may not read log files (HTTP 403) | The service key lacks the role for the LogFiles API. |
| Fetch reports "Import failed: … end-of-stream marker" | A downloaded file was incomplete; the next fetch retries it. |
| Requests return 503 "database busy" | Many slow queries at once; narrow the time range or search term, or raise `QUERY_TIMEOUT_S`. |
| `could not parse /config/tenants.jsonc` in the log | The seed file is not valid JSON with comments. |

## LLM / automation API

Two endpoints allow LLMs or scripts to query logs. Interactive API docs are at `/docs`.

`GET /api/query/schema` describes the query API, available filters and configured tenants.
Errors are HTTP status codes with a JSON body `{"detail": …}`: 404 unknown tenant, schedule, entry or
run; 409 a fetch is already running (with its `job_id`) or none is running; 422 invalid input
(`detail` lists the fields); 502 a connection test failed (with `kind`, `step`, `message` and `hint`
besides `detail`); 503 the database is busy (retry after the `Retry-After` seconds); 507 not enough
disk space for a backup.

`GET /api/fetch/runs` lists past fetch jobs (manual, demo or scheduled) with their status, times and
counters; a job cut off by a stop of the app is marked `interrupted`.
`GET /api/fetch/status` shows the current or last job with one entry per tenant and log type in
`parts` (status, files, imported entries, warnings, error) and the latest warnings and errors in
`problems`. A tenant that fails does not stop the job: it ends `done` with `errors` above 0.

`POST /api/query` searches log entries with structured filters and returns a natural-language
`summary` plus the matching `items`:

```bash
curl -X POST http://localhost:8080/api/query \
  -H "Content-Type: application/json" \
  -d '{"tenant": "dev", "level": "ERROR", "iflow": "MyIFlow", "grep": "authorization failed",
       "date_from": "2024-01-01 00:00:00", "date_to": "2024-01-31 23:59:59", "limit": 50}'
```

| Field | Type | Description |
|---|---|---|
| `tenant` | string | Tenant ID; omit for all tenants |
| `level` | string | `ERROR`, `WARN`, `INFO`, `DEBUG` |
| `iflow` | string | Partial IFlow name |
| `grep` | string | Case-insensitive text search in message and logger; defaults to the last 24 hours when no range is supplied |
| `date_from` / `date_to` | string | ISO date or datetime; bounds are inclusive and a bare `date_to` includes the full day |
| `limit` | int | Max entries returned (1–200, default 50) |

Log timestamps are stored as timezone-naive CPI timestamps and interpreted as UTC for filtering.
Timezone-less API datetimes are interpreted as UTC; offset-aware values are converted to UTC before
comparison. Browse date-time controls and quick ranges use UTC; their timezone-less ISO values in
the `#browse` URL mean the same instant in every browser. The URL also carries its filters, current
page (`page`), and opened entry (`entry`).

## Log format

CPI writes `#`-delimited lines with 15 fields:

```
Timestamp # Timezone # Level # Logger # User # Thread # Category # … # Message # - # IP # Node
```

Continuation lines (for example stack traces) are attached to the entry they belong to, and the
original line is kept alongside the parsed fields.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

```bash
docker compose -f docker-compose.yml -f compose.dev.yaml up --build   # live reload
```

Stack: Python, FastAPI, DuckDB, httpx · Alpine.js and Chart.js (vendored in `frontend/vendor/`),
Tailwind CSS 4 and daisyUI 5 (built from pinned standalone artifacts; no Node.js or npm needed).

## License

[MIT](LICENSE)
