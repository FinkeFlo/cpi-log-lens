"""Bring database files of app versions before versioned migrations to the baseline.

Each step checks the schema first and does nothing when it is already done, so on
current files the whole migration only records its version."""

import logging

import duckdb

log = logging.getLogger("cpi.db")

# Secondary indexes of old versions; they slowed down imports (ADR 0002) and
# block the column changes below.
_LEGACY_INDEXES = (
    "idx_logs_tenant",
    "idx_logs_level",
    "idx_logs_iflow",
    "idx_logs_timestamp",
    "idx_logs_tenant_ts",
    "idx_logs_level_ts",
    "idx_logs_tenant_level",
)


def upgrade(conn: duckdb.DuckDBPyConnection) -> None:
    for name in _LEGACY_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    _drop_logs_unique_constraint(conn)
    _timestamp_to_native(conn)
    _add_raw_line_column(conn)


def _drop_logs_unique_constraint(conn: duckdb.DuckDBPyConnection) -> None:
    """Old files enforce UNIQUE (tenant, log_type, filename, timestamp, level,
    logger, message) on logs, which made imports ever slower. A constraint cannot
    be dropped in place, so the table is rebuilt without it."""
    has_unique = conn.execute(
        "SELECT count(*) FROM duckdb_constraints() "
        "WHERE database_name = current_database() AND table_name = 'logs' AND constraint_type = 'UNIQUE'"
    ).fetchall()[0][0]
    if not has_unique:
        return
    log.info("migrate: rebuilding logs without the legacy UNIQUE constraint (this can take a while)")
    conn.execute("""
        CREATE TABLE logs_new (
            id          BIGINT PRIMARY KEY,
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
    # Old files have ids that did not come from logs_id_seq (the SQLite import
    # assigned them). DuckDB cannot restart a sequence, so it is recreated
    # past the highest id; nothing depends on it after the rebuild.
    max_id = conn.execute("SELECT coalesce(max(id), 0) FROM logs").fetchall()[0][0]
    last = conn.execute(
        "SELECT coalesce(max(last_value), 0) FROM duckdb_sequences() "
        "WHERE database_name = current_database() AND sequence_name = 'logs_id_seq'"
    ).fetchall()[0][0]
    conn.execute("DROP SEQUENCE IF EXISTS logs_id_seq")
    conn.execute(f"CREATE SEQUENCE logs_id_seq START WITH {max(max_id, last) + 1}")
    conn.execute("ALTER TABLE logs ALTER COLUMN id SET DEFAULT nextval('logs_id_seq')")


def _timestamp_to_native(conn: duckdb.DuckDBPyConnection) -> None:
    """Old files store logs.timestamp as TEXT; native TIMESTAMP enables zone-map
    pruning on time filters. All values were written by the parser in the strict
    'YYYY-MM-DD HH:MM:SS' format."""
    col_type = conn.execute(
        "SELECT data_type FROM duckdb_columns() "
        "WHERE database_name = current_database() AND table_name = 'logs' AND column_name = 'timestamp'"
    ).fetchall()
    if not col_type or col_type[0][0].upper() == "TIMESTAMP":
        return
    log.info("migrate: converting logs.timestamp from TEXT to TIMESTAMP")
    conn.execute("ALTER TABLE logs ALTER COLUMN timestamp TYPE TIMESTAMP USING CAST(timestamp AS TIMESTAMP)")


def _add_raw_line_column(conn: duckdb.DuckDBPyConnection) -> None:
    """Old files have no logs.raw_line; existing rows keep NULL."""
    has_col = conn.execute(
        "SELECT count(*) FROM duckdb_columns() "
        "WHERE database_name = current_database() AND table_name = 'logs' AND column_name = 'raw_line'"
    ).fetchall()[0][0]
    if has_col:
        return
    log.info("migrate: adding logs.raw_line")
    conn.execute("ALTER TABLE logs ADD COLUMN raw_line TEXT")
