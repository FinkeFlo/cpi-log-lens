"""Database schema and the ad-hoc migrations of older database files."""

import logging

import duckdb

log = logging.getLogger("cpi.db")

SCHEMA = """
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

-- NOTE: no secondary indexes and no UNIQUE constraint on `logs`.
-- Benchmarked: secondary indexes give negligible read speedup (<3% on a
-- 1M-row filter+sort query) while adding real per-row insert/maintenance
-- overhead that grows with table size. The UNIQUE constraint that used to
-- sit here (tenant, log_type, filename, timestamp, level, logger, message)
-- has the *same* problem but far worse: DuckDB maintains an ART index to
-- enforce it, and per-row conflict checking against that index gets
-- progressively slower as the table grows — measured degrading from ~9.5 to
-- ~3 files/hour (100% CPU) once `logs` passed ~5.8M rows. Duplicate
-- protection across fetch runs is instead provided entirely by
-- `file_imports.lines` (only rows beyond the last-imported line are ever
-- parsed/inserted into `import_log_file()`) — see migration below for how this
-- constraint is retroactively dropped from existing DB files.
DROP INDEX IF EXISTS idx_logs_tenant;
DROP INDEX IF EXISTS idx_logs_level;
DROP INDEX IF EXISTS idx_logs_iflow;
DROP INDEX IF EXISTS idx_logs_timestamp;
DROP INDEX IF EXISTS idx_logs_tenant_ts;
DROP INDEX IF EXISTS idx_logs_level_ts;
DROP INDEX IF EXISTS idx_logs_tenant_level;

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
-- last 1h"). Checked periodically by the background scheduler loop in
-- main.py. tenants/log_types are stored as JSON arrays (or '["all"]').
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
"""


def _migrate_drop_logs_unique_constraint(conn: duckdb.DuckDBPyConnection):
    """One-off migration for DBs created before the UNIQUE constraint on `logs`
    was removed (see comment in SCHEMA). `CREATE TABLE IF NOT EXISTS` never
    retroactively changes an existing table's constraints, so DBs created by an
    older version of this app keep the old, insert-performance-killing UNIQUE
    constraint forever unless we explicitly rebuild the table here. Idempotent:
    does nothing once the constraint is gone."""
    has_unique = conn.execute("""
        SELECT 1 FROM duckdb_constraints()
        WHERE table_name = 'logs' AND constraint_type = 'UNIQUE'
        LIMIT 1
    """).fetchone()
    if not has_unique:
        return

    log.info("migrate: Dropping legacy UNIQUE constraint on logs (rebuilding table)...")
    conn.execute("""
        CREATE TABLE logs_new (
            id          BIGINT PRIMARY KEY DEFAULT nextval('logs_id_seq'),
            tenant      TEXT NOT NULL,
            log_type    TEXT NOT NULL,
            filename    TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            level       TEXT,
            logger      TEXT,
            iflow       TEXT,
            message     TEXT,
            ip          TEXT,
            node        TEXT,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        INSERT INTO logs_new (id, tenant, log_type, filename, timestamp, level,
                               logger, iflow, message, ip, node, imported_at)
        SELECT id, tenant, log_type, filename, timestamp, level,
               logger, iflow, message, ip, node, imported_at
        FROM logs
    """)
    conn.execute("DROP TABLE logs")
    conn.execute("ALTER TABLE logs_new RENAME TO logs")
    # Keep the sequence ahead of the max copied id so future inserts don't collide.
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchall()[0][0]
    conn.execute(f"ALTER SEQUENCE logs_id_seq RESTART WITH {max_id + 1}")
    conn.execute("CHECKPOINT")
    log.info("migrate: Done — UNIQUE constraint removed from logs.")


def _migrate_timestamp_to_native(conn: duckdb.DuckDBPyConnection):
    """One-off migration for DBs created before `timestamp` was a native
    TIMESTAMP column (older versions stored it as TEXT). Native TIMESTAMP
    enables DuckDB zonemap pruning on time-range filters (used everywhere:
    query_logs, get_stats, cleanup_old_logs) and stores more compactly than
    the equivalent TEXT. Idempotent: no-op once the column is already
    TIMESTAMP. Safe to run on any size table — all values in `logs.timestamp`
    are written by iter_log_batches() in the strict 'YYYY-MM-DD HH:MM:SS'
    format (see LINE_RE), which DuckDB parses unambiguously."""
    col_type = conn.execute("""
        SELECT data_type FROM duckdb_columns()
        WHERE table_name = 'logs' AND column_name = 'timestamp'
    """).fetchone()
    if not col_type or col_type[0].upper() == "TIMESTAMP":
        return

    log.info("migrate: Converting logs.timestamp from TEXT to native TIMESTAMP...")
    conn.execute("ALTER TABLE logs ALTER COLUMN timestamp TYPE TIMESTAMP USING CAST(timestamp AS TIMESTAMP)")
    conn.execute("CHECKPOINT")
    log.info("migrate: Done — logs.timestamp is now native TIMESTAMP.")


def _migrate_add_raw_line_column(conn: duckdb.DuckDBPyConnection):
    """One-off migration for DBs created before `logs.raw_line` existed.
    `CREATE TABLE IF NOT EXISTS` never adds columns to an already-existing
    table, so this ALTER is needed for any DB file created by an older
    version of this app. Existing rows get raw_line=NULL (no backfill from
    the original log files — only newly imported rows get it populated).
    Idempotent: no-op once the column already exists."""
    has_col = conn.execute("""
        SELECT 1 FROM duckdb_columns()
        WHERE table_name = 'logs' AND column_name = 'raw_line'
    """).fetchone()
    if has_col:
        return

    log.info("migrate: Adding logs.raw_line column...")
    conn.execute("ALTER TABLE logs ADD COLUMN raw_line TEXT")
    conn.execute("CHECKPOINT")
    log.info("migrate: Done — logs.raw_line added (NULL for pre-existing rows).")


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    _migrate_drop_logs_unique_constraint(conn)
    _migrate_timestamp_to_native(conn)
    _migrate_add_raw_line_column(conn)
