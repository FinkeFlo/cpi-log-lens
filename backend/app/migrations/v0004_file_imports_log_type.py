"""Key the import bookkeeping by log type as well: (tenant, log_type, filename).

Before, a trace and an HTTP log file with the same name shared one offset, so the
second one was skipped. Existing rows get the log type of their imported rows;
rows whose log entries are gone (deleted by retention) get it from the file name
(CPI names files after their type, e.g. trace_<time>.log), otherwise "trace".
"""

import logging

import duckdb

log = logging.getLogger("cpi.db")


def upgrade(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE file_imports_new (
            tenant   TEXT NOT NULL,
            log_type TEXT NOT NULL,
            filename TEXT NOT NULL,
            lines    INTEGER NOT NULL DEFAULT 0,
            size     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant, log_type, filename)
        )
    """)
    conn.execute("""
        INSERT INTO file_imports_new (tenant, log_type, filename, lines, size)
        SELECT f.tenant,
               coalesce(
                   l.log_type,
                   CASE WHEN lower(f.filename) LIKE 'http%' THEN 'http' ELSE 'trace' END
               ),
               f.filename, f.lines, f.size
        FROM file_imports f
        LEFT JOIN (
            -- Before this change a file name had at most one offset; if its rows
            -- span two log types (impossible unless written by hand), take one.
            SELECT tenant, filename, min(log_type) AS log_type
            FROM logs GROUP BY tenant, filename
        ) l USING (tenant, filename)
    """)
    moved = conn.execute("SELECT count(*) FROM file_imports_new").fetchall()[0][0]
    conn.execute("DROP TABLE file_imports")
    conn.execute("ALTER TABLE file_imports_new RENAME TO file_imports")
    log.info("migrate: file_imports keyed by log type (%d rows)", moved)
