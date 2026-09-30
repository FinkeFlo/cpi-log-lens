"""Versioned schema migrations.

Every file in this package named ``vNNNN_<name>.sql`` or ``vNNNN_<name>.py`` (with an
``upgrade(conn)`` function) is one migration. They run in order of NNNN at start-up,
each in its own transaction together with its row in ``schema_version``, so a failed
migration leaves no trace and runs again on the next start. Migrations that have run
are never changed; a schema change is a new file with the next number.

Files from app versions before this table existed have no ``schema_version``; they
run the idempotent baseline (0001) and the legacy fix-ups (0002) like a new file.
A database whose version is higher than the newest migration known to this app was
written by a newer app version: the app refuses to start instead of guessing.
"""

import importlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb

log = logging.getLogger("cpi.db")

_FILE_RE = re.compile(r"^v(\d{4})_(\w+)\.(sql|py)$")


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer version of the app."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[duckdb.DuckDBPyConnection], None]


def _sql_migration(sql: str) -> Callable[[duckdb.DuckDBPyConnection], None]:
    def apply(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(sql)

    return apply


def discover() -> list[Migration]:
    migrations = []
    for path in Path(__file__).parent.iterdir():
        m = _FILE_RE.match(path.name)
        if not m:
            continue
        version, name, kind = int(m.group(1)), m.group(2), m.group(3)
        if kind == "sql":
            apply = _sql_migration(path.read_text())
        else:
            apply = importlib.import_module(f"{__name__}.{path.stem}").upgrade
        migrations.append(Migration(version, name, apply))
    migrations.sort(key=lambda m: m.version)
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration numbers: {versions}")
    return migrations


def current_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Highest applied migration (0 for a database without schema_version)."""
    if not _has_table(conn, "schema_version"):
        return 0
    return conn.execute("SELECT coalesce(max(version), 0) FROM schema_version").fetchall()[0][0]


def migrate(
    conn: duckdb.DuckDBPyConnection, app_version: str = "dev", migrations: list[Migration] | None = None
) -> list[int]:
    """Apply all pending migrations; returns the versions that were applied."""
    migrations = discover() if migrations is None else migrations
    head = migrations[-1].version if migrations else 0
    existing = _has_table(conn, "logs")
    current = current_version(conn)
    if current > head:
        raise SchemaTooNewError(
            f"The database schema is at version {current}, but this app only knows versions up to {head}: "
            "it was written by a newer app version. Use that version (or newer), or restore a backup "
            "made before the upgrade."
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            app_version TEXT
        )
    """)
    applied = {v for (v,) in conn.execute("SELECT version FROM schema_version").fetchall()}
    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        log.info("database schema is up to date (version %d)", current)
        return []
    if existing and not applied:
        log.info("database from an app version without schema versions: applying baseline and fix-ups")
    for m in pending:
        t0 = time.perf_counter()
        log.info("migration %04d %s: applying", m.version, m.name)
        conn.begin()
        try:
            m.apply(conn)
            conn.execute(
                "INSERT INTO schema_version (version, name, app_version) VALUES (?, ?, ?)",
                [m.version, m.name, app_version],
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            log.error("migration %04d %s failed; the database is unchanged", m.version, m.name)
            raise
        log.info("migration %04d %s: done in %.1fs", m.version, m.name, time.perf_counter() - t0)
    conn.execute("CHECKPOINT")
    return [m.version for m in pending]


def _has_table(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = 'main' AND table_name = ?",
            [name],
        ).fetchall()[0][0]
    )
