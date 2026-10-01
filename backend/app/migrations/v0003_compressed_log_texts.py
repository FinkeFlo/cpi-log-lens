"""Declare ZSTD compression for the log texts (message, raw_line) where the file supports it.

New database files are created in storage version v1.5.0, so their (still empty) logs
table is recreated with the compressed columns. Files in an older storage version
cannot use ZSTD (a declared ZSTD column would silently stay uncompressed); they keep
their layout until the storage upgrade (DB_STORAGE_UPGRADE) copies them into a new file.
"""

import logging

import duckdb

from app import storage

log = logging.getLogger("cpi.db")


def upgrade(conn: duckdb.DuckDBPyConnection) -> None:
    if not storage.supports_zstd(conn):
        log.info(
            "migrate: the database file uses storage format %s without ZSTD support; "
            "set DB_STORAGE_UPGRADE=true to convert it and make the log texts smaller",
            storage.storage_version(conn),
        )
        return
    if storage.texts_compressed(conn) and conn.execute("SELECT count(*) FROM logs").fetchall()[0][0]:
        return  # already compressed (e.g. by the storage upgrade)
    create_sql = conn.execute(
        "SELECT sql FROM duckdb_tables() WHERE database_name = current_database() AND table_name = 'logs'"
    ).fetchall()[0][0]
    log.info("migrate: rewriting logs with ZSTD-compressed text columns")
    conn.execute("ALTER TABLE logs RENAME TO logs_uncompressed")
    conn.execute(storage.with_compressed_texts(create_sql))
    conn.execute("INSERT INTO logs SELECT * FROM logs_uncompressed ORDER BY timestamp, id")
    conn.execute("DROP TABLE logs_uncompressed")
