-- Baseline schema: the database as created by the app before versioned
-- migrations. Idempotent (IF NOT EXISTS), so it also completes older files.
CREATE TABLE IF NOT EXISTS tenants (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    api_url       TEXT NOT NULL,
    oauth_url     TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS logs_id_seq;

CREATE TABLE IF NOT EXISTS logs (
    id          BIGINT PRIMARY KEY DEFAULT nextval('logs_id_seq'),
    tenant      TEXT NOT NULL,
    log_type    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    timestamp   TIMESTAMP NOT NULL,
    level       TEXT,
    logger      TEXT,
    iflow       TEXT,
    message     TEXT,
    ip          TEXT,
    node        TEXT,
    raw_line    TEXT,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- No secondary indexes and no UNIQUE constraint on `logs` (ADR 0002):
-- secondary indexes gave <3% read speedup on a 1M-row filter+sort query but
-- cost per-row insert/maintenance time that grows with the table, and the
-- former UNIQUE constraint (tenant, log_type, filename, timestamp, level,
-- logger, message) made imports degrade from ~9.5 to ~3 files/hour at ~5.8M
-- rows. Duplicate protection comes from `file_imports.lines`: only rows past
-- the last imported row of a file are inserted.

CREATE TABLE IF NOT EXISTS file_imports (
    tenant   TEXT NOT NULL,
    filename TEXT NOT NULL,
    lines    INTEGER NOT NULL DEFAULT 0,
    size     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, filename)
);

CREATE SEQUENCE IF NOT EXISTS fetch_runs_id_seq;

CREATE TABLE IF NOT EXISTS fetch_runs (
    id               BIGINT PRIMARY KEY DEFAULT nextval('fetch_runs_id_seq'),
    tenant           TEXT,
    log_type         TEXT,
    started_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at      TEXT,
    files_total      INTEGER DEFAULT 0,
    files_done       INTEGER DEFAULT 0,
    entries_imported INTEGER DEFAULT 0,
    status           TEXT DEFAULT 'running'
);

-- Recurring fetch configurations (e.g. "pull tenant X every 15 min for the
-- last 1h"), checked by the scheduler. tenants/log_types are JSON arrays
-- (or '["all"]').
CREATE TABLE IF NOT EXISTS fetch_schedules (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    tenants           TEXT NOT NULL,   -- JSON array of tenant ids, or '["all"]'
    log_types         TEXT NOT NULL,   -- JSON array, e.g. '["trace","http"]'
    hours             INTEGER NOT NULL DEFAULT 24,
    interval_minutes  INTEGER NOT NULL DEFAULT 15,
    enabled           BOOLEAN NOT NULL DEFAULT true,
    last_run_at       TEXT,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Generic key/value app settings (e.g. the last-used fetch form config,
-- saved as a default so the UI doesn't always start from "all tenants").
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS unparsed_lines_id_seq;

-- Lines that iter_log_batches() could neither match against LINE_RE nor
-- attach as a continuation of the previous parsed row (i.e. the very first
-- line of a file/parse run is itself unparsable, so there is no prior
-- message to append it to). Kept here instead of being silently dropped so
-- log imports stay recoverable/auditable even for unexpected line formats.
CREATE TABLE IF NOT EXISTS unparsed_lines (
    id          BIGINT PRIMARY KEY DEFAULT nextval('unparsed_lines_id_seq'),
    tenant      TEXT NOT NULL,
    log_type    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    raw_text    TEXT,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);
