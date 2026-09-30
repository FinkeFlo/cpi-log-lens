"""Database file storage: format checks, the compressed-storage upgrade and backups.

DuckDB compresses long text columns with FSST by default. ZSTD makes the log texts
(message, raw_line) several times smaller, but only in files with storage version
v1.2.0 or newer; in older files a column declared ZSTD silently stays uncompressed.
The storage version of a file is fixed when it is created, so an existing file is
upgraded by copying everything into a new file and swapping the files.

A file in storage version v1.5.0 can only be opened by DuckDB 1.5 or newer.
"""

import logging
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

log = logging.getLogger("cpi.db")

# Storage version of new database files and of upgraded ones.
TARGET_STORAGE_VERSION = "v1.5.0"
# Oldest storage version in which ZSTD is used for a column declared with it.
_ZSTD_MIN_VERSION = (1, 2, 0)
COMPRESSED_COLUMNS = ("message", "raw_line")
# Compression types that count as "compressed as intended" for those columns
# (Constant: a segment with one repeated value, e.g. all NULL).
_OK_COMPRESSION = {"ZSTD", "Constant"}


class StorageUpgradeError(RuntimeError):
    """The storage upgrade could not be done; the database file is unchanged."""


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_version(tag: str) -> tuple[int, ...]:
    """'v1.0.0+' -> (1, 0, 0)"""
    return tuple(int(x) for x in re.findall(r"\d+", tag)[:3])


def storage_version(conn: duckdb.DuckDBPyConnection, database: str | None = None) -> str:
    """Storage version tag of an attached database, e.g. 'v1.0.0+' (default: the current one)."""
    row = conn.execute(
        "SELECT tags['storage_version'] FROM duckdb_databases() WHERE database_name = coalesce(?, current_database())",
        [database],
    ).fetchall()
    return row[0][0] if row and row[0][0] else "v0.0.0"


def supports_zstd(conn: duckdb.DuckDBPyConnection, database: str | None = None) -> bool:
    return parse_version(storage_version(conn, database)) >= _ZSTD_MIN_VERSION


def with_compressed_texts(create_sql: str) -> str:
    """The CREATE TABLE statement of logs with ZSTD declared for the text columns."""
    sql = create_sql
    for column in COMPRESSED_COLUMNS:
        pattern = rf"\b{column} VARCHAR\b(?! USING COMPRESSION)"
        sql, n = re.subn(pattern, f"{column} VARCHAR USING COMPRESSION zstd", sql)
        if n != 1:
            raise StorageUpgradeError(f"unexpected definition of logs.{column}: {create_sql}")
    return sql


def texts_compressed(conn: duckdb.DuckDBPyConnection, table: str = "logs") -> bool:
    """True when every stored segment of the log text columns is ZSTD (or constant)."""
    rows = conn.execute(
        "SELECT DISTINCT compression FROM pragma_storage_info(?) "
        "WHERE column_name IN ('message', 'raw_line') AND segment_type = 'VARCHAR'",
        [table],
    ).fetchall()
    return {r[0] for r in rows} <= _OK_COMPRESSION


def needs_upgrade(path: Path) -> bool:
    """Whether the file would get smaller from the compressed-storage upgrade."""
    with duckdb.connect(str(path), read_only=True) as conn:
        if not supports_zstd(conn):
            return True
        has_logs = conn.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database() AND table_name = 'logs'"
        ).fetchall()[0][0]
        return bool(has_logs) and not texts_compressed(conn)


def _tables(conn: duckdb.DuckDBPyConnection, database: str) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = ? AND schema_name = 'main' ORDER BY table_name",
        [database],
    ).fetchall()
    return [r[0] for r in rows]


def _count(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]


def upgrade_file(path: Path, *, memory_limit: str, threads: int, temp_dir: str = "") -> Path:
    """Rewrite the database at `path` into storage version v1.5.0 with ZSTD for the log
    texts and the log rows sorted by time, then swap the files. Every table, sequence
    and row is copied and verified before the swap. The old file is kept next to the
    new one as `<name>.bak-<UTC time>`; its path is returned.

    Must run while no connection has the file open. Raises StorageUpgradeError (with
    the file unchanged) when something is off, e.g. not enough free disk space."""
    t_start = time.perf_counter()
    wal = path.with_name(path.name + ".wal")
    size = path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    free = shutil.disk_usage(path.parent).free
    if free < size * 1.1:
        raise StorageUpgradeError(f"not enough free disk space: the copy needs up to {size} bytes, {free} are free")

    config: dict[str, str | bool | int | float | list[str]] = {"memory_limit": memory_limit, "threads": threads}
    config["temp_directory"] = temp_dir or str(path) + ".tmp"
    # Fold a WAL left behind by a crash into the file, so the copy sees everything.
    with duckdb.connect(str(path), config=config) as conn:
        conn.execute("CHECKPOINT")

    new = path.with_name(path.name + ".upgrade")
    for leftover in (new, new.with_name(new.name + ".wal")):
        leftover.unlink(missing_ok=True)

    conn = duckdb.connect(config=config)
    try:
        conn.execute(f"ATTACH {_quote(str(path))} AS old_db (READ_ONLY)")
        conn.execute(f"ATTACH {_quote(str(new))} AS new_db (STORAGE_VERSION '{TARGET_STORAGE_VERSION}')")
        log.info(
            "storage upgrade: %s (%s, %.1f MB) -> %s",
            path.name,
            storage_version(conn, "old_db"),
            size / 1e6,
            TARGET_STORAGE_VERSION,
        )
        tables = _tables(conn, "old_db")
        if "logs" not in tables:
            raise StorageUpgradeError("the database has no logs table")
        # Tables (with constraints and defaults), sequences with their counters, …
        conn.execute("COPY FROM DATABASE old_db TO new_db (SCHEMA)")
        logs_sql = conn.execute(
            "SELECT sql FROM duckdb_tables() WHERE database_name = 'new_db' AND table_name = 'logs'"
        ).fetchall()[0][0]
        conn.execute("USE new_db")
        # … but logs is recreated with compressed text columns, and its sequence
        # restarts past the highest id (older files may have ids from elsewhere).
        conn.execute("DROP TABLE logs")
        max_id = conn.execute("SELECT coalesce(max(id), 0) FROM old_db.logs").fetchall()[0][0]
        last = conn.execute(
            "SELECT coalesce(max(last_value), 0) FROM duckdb_sequences() "
            "WHERE database_name = 'old_db' AND sequence_name = 'logs_id_seq'"
        ).fetchall()[0][0]
        conn.execute("DROP SEQUENCE IF EXISTS logs_id_seq")
        conn.execute(f"CREATE SEQUENCE logs_id_seq START WITH {max(max_id, last) + 1}")
        conn.execute(with_compressed_texts(logs_sql))

        for table in tables:
            t0 = time.perf_counter()
            order = " ORDER BY timestamp, id" if table == "logs" else ""
            conn.execute(f"INSERT INTO new_db.{table} SELECT * FROM old_db.{table}{order}")
            log.info("storage upgrade: copied %s in %.1fs", table, time.perf_counter() - t0)
        conn.execute("CHECKPOINT new_db")

        for table in tables:
            before, after = _count(conn, f"old_db.{table}"), _count(conn, f"new_db.{table}")
            if before != after:
                raise StorageUpgradeError(f"row count of {table} differs after the copy: {before} vs {after}")
        if _count(conn, "new_db.logs") and not texts_compressed(conn, "new_db.logs"):
            raise StorageUpgradeError("the log texts in the new file are not ZSTD-compressed")
        conn.execute("USE memory")
        conn.execute("DETACH new_db")
        conn.execute("DETACH old_db")
    except BaseException as e:
        conn.close()
        for leftover in (new, new.with_name(new.name + ".wal")):
            leftover.unlink(missing_ok=True)
        if isinstance(e, StorageUpgradeError):
            raise
        raise StorageUpgradeError(f"copy failed: {e}") from e
    conn.close()

    # Swap: keep the old file under a second name (a hard link, no copy), then
    # atomically put the new file in place. A crash in between leaves the old file.
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    os.link(path, backup)
    os.replace(new, path)
    log.info(
        "storage upgrade: done in %.0fs, %.1f MB -> %.1f MB; the old file is kept as %s",
        time.perf_counter() - t_start,
        size / 1e6,
        path.stat().st_size / 1e6,
        backup.name,
    )
    return backup


def backup_to(conn: duckdb.DuckDBPyConnection, dest: Path) -> None:
    """Copy the whole open database (all tables, sequences, compression settings) into
    a new file `dest` with the same storage version. Runs on the writer connection."""
    part = dest.with_name(dest.name + ".partial")
    part.unlink(missing_ok=True)
    source = conn.execute("SELECT current_database()").fetchall()[0][0]
    version = "v" + ".".join(str(n) for n in parse_version(storage_version(conn)))
    conn.execute(f"ATTACH {_quote(str(part))} AS backup_db (STORAGE_VERSION '{version}')")
    try:
        conn.execute(f'COPY FROM DATABASE "{source}" TO backup_db')
    except BaseException:
        conn.execute("DETACH backup_db")
        part.unlink(missing_ok=True)
        raise
    conn.execute("DETACH backup_db")
    os.replace(part, dest)


def main() -> int:
    """`python -m app.storage`: convert the database file to the compressed storage
    format now, instead of on the next start with DB_STORAGE_UPGRADE=true. The app
    must not be running (DuckDB locks the file)."""
    from app.config import get_settings
    from app.logging_config import setup_logging

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    path = settings.db_path
    if not path.exists():
        log.error("no database at %s", path)
        return 1
    try:
        if not needs_upgrade(path):
            log.info("%s already uses the compressed storage format", path)
            return 0
        upgrade_file(
            path,
            memory_limit=settings.duckdb_memory_limit,
            threads=settings.duckdb_threads,
            temp_dir=settings.duckdb_temp_dir,
        )
    except (StorageUpgradeError, duckdb.IOException) as e:
        log.error("storage upgrade failed, the database is unchanged: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
