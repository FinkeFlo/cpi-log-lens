"""Versioned schema migrations: new, legacy and current databases, idempotence, and the
guard against databases written by a newer app version."""

import logging

import duckdb
import pytest

from app import migrations
from app.migrations import Migration, SchemaTooNewError
from app.repositories.database import Database

EXPECTED_TABLES = {
    "tenants",
    "logs",
    "file_imports",
    "fetch_runs",
    "fetch_schedules",
    "settings",
    "unparsed_lines",
    "schema_version",
}


def head() -> int:
    return migrations.discover()[-1].version


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()}


def versions(conn) -> list[int]:
    return [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()]


def logs_columns(conn) -> dict[str, str]:
    rows = conn.execute("SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = 'logs'").fetchall()
    return dict(rows)


def create_legacy_db(conn) -> None:
    """A database as written by the first DuckDB versions of the app (after the
    import from SQLite): UNIQUE constraint and indexes on logs, TEXT timestamps,
    no raw_line, ids assigned by the import instead of logs_id_seq."""
    conn.execute("""
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, api_url TEXT NOT NULL, oauth_url TEXT NOT NULL,
            client_id TEXT NOT NULL, client_secret TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE logs (
            id BIGINT PRIMARY KEY, tenant TEXT NOT NULL, log_type TEXT NOT NULL, filename TEXT NOT NULL,
            timestamp TEXT NOT NULL, level TEXT, logger TEXT, iflow TEXT, message TEXT, ip TEXT, node TEXT,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant, log_type, filename, timestamp, level, logger, message)
        )
    """)
    conn.execute("CREATE INDEX idx_logs_tenant ON logs(tenant)")
    conn.execute("CREATE INDEX idx_logs_timestamp ON logs(timestamp)")
    conn.execute("""
        CREATE TABLE file_imports (
            tenant TEXT NOT NULL, filename TEXT NOT NULL, lines INTEGER NOT NULL DEFAULT 0,
            size INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (tenant, filename)
        )
    """)
    conn.execute("CREATE SEQUENCE logs_id_seq")
    conn.execute(
        "INSERT INTO tenants (id, name, api_url, oauth_url, client_id, client_secret) VALUES "
        "('dev', 'DEV', 'https://x.example', 'https://x.example/t', 'c', 's')"
    )
    conn.execute("""
        INSERT INTO logs (id, tenant, log_type, filename, timestamp, level, logger, iflow, message, ip, node)
        VALUES (500, 'dev', 'trace', 'a.log', '2026-01-15 08:00:00', 'INFO', 'L', 'F', 'first', '192.0.2.1', '1'),
               (900, 'dev', 'trace', 'a.log', '2026-01-15 08:00:01', 'ERROR', 'L', 'F', 'second', '192.0.2.1', '1')
    """)
    conn.execute("INSERT INTO file_imports VALUES ('dev', 'a.log', 2, 1234)")


def test_new_database_gets_every_migration(tmp_path):
    with duckdb.connect(str(tmp_path / "new.duckdb")) as conn:
        applied = migrations.migrate(conn, "1.2.3")
        assert applied == [m.version for m in migrations.discover()]
        assert tables(conn) == EXPECTED_TABLES
        assert versions(conn) == applied
        assert conn.execute("SELECT DISTINCT app_version FROM schema_version").fetchall() == [("1.2.3",)]
        assert migrations.current_version(conn) == head()


def test_second_run_is_a_no_op(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    path = str(tmp_path / "db.duckdb")
    with duckdb.connect(path) as conn:
        migrations.migrate(conn)
    with duckdb.connect(path) as conn:
        assert migrations.migrate(conn) == []
        assert versions(conn) == [m.version for m in migrations.discover()]
    assert "database schema is up to date" in caplog.text


def test_legacy_database_is_brought_to_the_baseline(tmp_path):
    path = str(tmp_path / "legacy.duckdb")
    with duckdb.connect(path) as conn:
        create_legacy_db(conn)
    with duckdb.connect(path) as conn:
        migrations.migrate(conn)
        assert tables(conn) == EXPECTED_TABLES
        unique = conn.execute(
            "SELECT count(*) FROM duckdb_constraints() WHERE table_name = 'logs' AND constraint_type = 'UNIQUE'"
        ).fetchall()
        assert unique == [(0,)]
        assert conn.execute("SELECT count(*) FROM duckdb_indexes()").fetchall() == [(0,)]
        columns = logs_columns(conn)
        assert columns["timestamp"] == "TIMESTAMP"
        assert "raw_line" in columns
        # Data is kept, including ids and the import bookkeeping.
        rows = conn.execute("SELECT id, timestamp::VARCHAR, message, raw_line FROM logs ORDER BY id").fetchall()
        assert rows == [(500, "2026-01-15 08:00:00", "first", None), (900, "2026-01-15 08:00:01", "second", None)]
        assert conn.execute("SELECT * FROM file_imports").fetchall() == [("dev", "a.log", 2, 1234)]
        assert conn.execute("SELECT id FROM tenants").fetchall() == [("dev",)]
        # New rows get ids past the imported ones.
        conn.execute("INSERT INTO logs (tenant, log_type, filename, timestamp) VALUES ('dev', 'trace', 'b', now())")
        assert conn.execute("SELECT max(id) FROM logs").fetchall() == [(901,)]


def test_current_database_without_versions_keeps_its_data(tmp_path):
    """A file of the last app version before versioned migrations: current schema, no schema_version."""
    path = str(tmp_path / "current.duckdb")
    with duckdb.connect(path) as conn:
        migrations.discover()[0].apply(conn)  # the baseline, as the old start-up code created it
        conn.execute(
            "INSERT INTO logs (tenant, log_type, filename, timestamp) VALUES ('t', 'trace', 'a', now()::TIMESTAMP)"
        )
        before = conn.execute("SELECT * FROM logs").fetchall()
    with duckdb.connect(path) as conn:
        migrations.migrate(conn)
        assert versions(conn) == [m.version for m in migrations.discover()]
        assert conn.execute("SELECT * FROM logs").fetchall() == before


def test_database_from_a_newer_app_is_refused(tmp_path):
    path = str(tmp_path / "newer.duckdb")
    with duckdb.connect(path) as conn:
        migrations.migrate(conn)
        conn.execute("INSERT INTO schema_version (version, name) VALUES (?, 'from the future')", [head() + 1])
    with duckdb.connect(path) as conn, pytest.raises(SchemaTooNewError, match="newer app version"):
        migrations.migrate(conn)


def test_failed_migration_is_rolled_back_and_retried(tmp_path):
    def broken(conn):
        conn.execute("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("boom")

    path = str(tmp_path / "db.duckdb")
    known = migrations.discover()
    with duckdb.connect(path) as conn:
        migrations.migrate(conn)
        with pytest.raises(RuntimeError, match="boom"):
            migrations.migrate(conn, migrations=[*known, Migration(head() + 1, "broken", broken)])
        assert "half_done" not in tables(conn)
        assert migrations.current_version(conn) == head()

        def fixed_upgrade(conn):
            conn.execute("CREATE TABLE half_done (x INTEGER)")

        fixed = Migration(head() + 1, "fixed", fixed_upgrade)
        assert migrations.migrate(conn, migrations=[*known, fixed]) == [head() + 1]
        assert "half_done" in tables(conn)


def test_migration_files_are_numbered_without_gaps():
    numbers = [m.version for m in migrations.discover()]
    assert numbers == list(range(1, len(numbers) + 1))


def test_app_start_refuses_a_newer_database(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "newer.duckdb")
    with duckdb.connect(str(settings.db_path)) as conn:
        migrations.migrate(conn)
        conn.execute("INSERT INTO schema_version (version, name) VALUES (?, 'from the future')", [head() + 1])
    with pytest.raises(SchemaTooNewError):
        Database.open(settings.db_path)
    # The file is not left locked.
    duckdb.connect(str(settings.db_path)).close()
