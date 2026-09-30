"""Database file storage: format checks and backups."""

import os
import re
from pathlib import Path

import duckdb


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
