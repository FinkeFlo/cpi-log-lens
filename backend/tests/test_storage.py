"""Storage format: compressed log texts in new files, the opt-in upgrade of existing
files, and database backups."""

import asyncio
import logging
import os
import shutil
from collections import namedtuple

import duckdb
import pytest
from asgi_lifespan import LifespanManager

from app import logging_config, main, migrations, storage
from app.repositories.database import Database
from app.services import importer
from tests.support import FAKE_TENANT, app_db, log_line, numbered_lines, wait_for_job, write_log

pytestmark = pytest.mark.anyio

ALL_TABLES = [
    "fetch_runs",
    "fetch_schedules",
    "file_imports",
    "logs",
    "schema_version",
    "settings",
    "tenants",
    "unparsed_lines",
]


def counts(conn, database=None) -> dict[str, int]:
    prefix = f"{database}." if database else ""
    names = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = coalesce(?, current_database()) "
            "ORDER BY table_name",
            [database],
        ).fetchall()
    ]
    return {t: conn.execute(f"SELECT count(*) FROM {prefix}{t}").fetchall()[0][0] for t in names}


def text_compression(conn) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT compression FROM pragma_storage_info('logs') "
        "WHERE column_name IN ('message', 'raw_line') AND segment_type = 'VARCHAR'"
    ).fetchall()
    return {r[0] for r in rows}


def make_old_format_db(path, rows=3000) -> None:
    """A database as written before this change: default (old) storage version,
    uncompressed-FSST log texts, rows not in time order, a sequence behind max(id)."""
    with duckdb.connect(str(path)) as conn:  # no storage_compatibility_version: old format
        migrations.migrate(conn)
        conn.execute(
            "INSERT INTO tenants (id, name, api_url, oauth_url, client_id, client_secret) VALUES "
            "('dev', 'DEV', 'https://x.example', 'https://x.example/t', 'c', 's')"
        )
        conn.execute(f"""
            INSERT INTO logs (tenant, log_type, filename, timestamp, level, message, raw_line)
            SELECT 'dev', 'trace', 'f' || (i % 7) || '.log',
                   TIMESTAMP '2026-01-15 00:00:00' + to_seconds((i * 7919) % {rows}),
                   'INFO', 'message number ' || i || ' of a repeated log text', 'raw ' || i
            FROM range({rows}) r(i)
        """)
        conn.execute(
            "INSERT INTO logs (id, tenant, log_type, filename, timestamp) VALUES (50000, 'dev', 'trace', 'x', now())"
        )
        conn.execute("INSERT INTO file_imports VALUES ('dev', 'f1.log', 10, 1234)")
        conn.execute(
            "INSERT INTO unparsed_lines (tenant, log_type, filename, line_no, raw_text) "
            "VALUES ('dev', 'trace', 'f1.log', 1, 'x')"
        )
        conn.execute("INSERT INTO settings VALUES ('k', 'v')")
        conn.execute(
            "INSERT INTO fetch_schedules (id, name, tenants, log_types) VALUES ('s1', 'n', '[\"all\"]', '[\"trace\"]')"
        )
        conn.execute("CHECKPOINT")


def upgrade(path):
    return storage.upgrade_file(path, memory_limit="1GB", threads=2)


# ── New files ────────────────────────────────────────────────────────────────


async def test_new_database_uses_compressed_log_texts(db, settings, tmp_path):
    path = write_log(tmp_path / "a.log", numbered_lines(2000))
    await importer.import_log_file(db, "t1", "trace", path, "a.log", 0)
    await db.run(lambda conn: conn.execute("CHECKPOINT"))
    await db.close()
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        assert storage.storage_version(conn) == "v1.5.0+"
        assert text_compression(conn) <= {"ZSTD", "Constant"}
        assert "ZSTD" in text_compression(conn)
    assert not storage.needs_upgrade(settings.db_path)


# ── Upgrade of existing files ─────────────────────────────────────────────────


def test_upgrade_copies_everything_and_compresses(tmp_path):
    path = tmp_path / "old.duckdb"
    make_old_format_db(path)
    with duckdb.connect(str(path), read_only=True) as conn:
        before = counts(conn)
        old_rows = conn.execute("SELECT * FROM logs ORDER BY id").fetchall()
        assert storage.storage_version(conn) == "v1.0.0+"
    assert storage.needs_upgrade(path)

    backup = upgrade(path)

    assert backup.exists() and backup.name.startswith("old.duckdb.bak-")
    assert not path.with_name("old.duckdb.upgrade").exists()
    with duckdb.connect(str(path)) as conn:
        assert storage.storage_version(conn) == "v1.5.0+"
        assert counts(conn) == before
        assert sorted(counts(conn)) == ALL_TABLES
        assert conn.execute("SELECT * FROM logs ORDER BY id").fetchall() == old_rows
        assert text_compression(conn) == {"ZSTD"}
        # Rows are stored in time order.
        stored = [r[0] for r in conn.execute("SELECT timestamp FROM logs").fetchall()]
        assert stored == sorted(stored)
        # New rows continue after the highest id, even though the old sequence was behind.
        conn.execute("INSERT INTO logs (tenant, log_type, filename, timestamp) VALUES ('dev', 'trace', 'y', now())")
        assert conn.execute("SELECT max(id) FROM logs").fetchall() == [(50001,)]
        conn.execute("INSERT INTO unparsed_lines (tenant, log_type, filename, line_no) VALUES ('d', 't', 'f', 2)")
    # The kept file is the untouched old one.
    with duckdb.connect(str(backup), read_only=True) as conn:
        assert storage.storage_version(conn) == "v1.0.0+"
        assert counts(conn) == before
    assert not storage.needs_upgrade(path)


def test_upgrade_needs_free_disk_space(tmp_path, monkeypatch):
    path = tmp_path / "old.duckdb"
    make_old_format_db(path, rows=10)
    mtime = path.stat().st_mtime_ns
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**9, 10**9, 1000))
    with pytest.raises(storage.StorageUpgradeError, match="not enough free disk space"):
        upgrade(path)
    assert path.stat().st_mtime_ns == mtime
    assert [p.name for p in tmp_path.iterdir()] == ["old.duckdb"]


def test_failed_upgrade_leaves_the_file_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "old.duckdb"
    make_old_format_db(path, rows=10)

    def broken(sql):
        raise storage.StorageUpgradeError("simulated")

    monkeypatch.setattr(storage, "with_compressed_texts", broken)
    with pytest.raises(storage.StorageUpgradeError, match="simulated"):
        upgrade(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old.duckdb"]
    with duckdb.connect(str(path), read_only=True) as conn:
        assert storage.storage_version(conn) == "v1.0.0+"


def test_with_compressed_texts():
    sql = "CREATE TABLE logs(id BIGINT, message VARCHAR, raw_line VARCHAR, imported_at VARCHAR);"
    assert storage.with_compressed_texts(sql) == (
        "CREATE TABLE logs(id BIGINT, message VARCHAR USING COMPRESSION zstd, "
        "raw_line VARCHAR USING COMPRESSION zstd, imported_at VARCHAR);"
    )
    with pytest.raises(storage.StorageUpgradeError):
        storage.with_compressed_texts("CREATE TABLE logs(id BIGINT, message TEXT);")


@pytest.mark.parametrize("enabled", [True, False])
async def test_upgrade_at_start_is_opt_in(app_env, settings, monkeypatch, enabled):
    make_old_format_db(settings.db_path, rows=100)
    monkeypatch.setattr(settings, "db_storage_upgrade", enabled)
    async with LifespanManager(main.app):
        pass
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        assert storage.storage_version(conn) == ("v1.5.0+" if enabled else "v1.0.0+")
        assert counts(conn)["logs"] == 101
    backups = [p for p in settings.db_path.parent.iterdir() if ".bak-" in p.name]
    assert len(backups) == (1 if enabled else 0)


async def test_old_format_file_keeps_its_layout_without_the_upgrade(app_env, settings):
    make_old_format_db(settings.db_path, rows=100)
    db = Database.open(settings.db_path)
    await db.close()
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        assert migrations.current_version(conn) == migrations.discover()[-1].version
        assert "ZSTD" not in text_compression(conn)
    assert storage.needs_upgrade(settings.db_path)


def test_command_line_upgrade(tmp_path, settings, monkeypatch, caplog):
    monkeypatch.setattr(logging_config, "setup_logging", lambda *args: None)
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(settings, "db_path", tmp_path / "old.duckdb")
    make_old_format_db(settings.db_path, rows=10)
    assert storage.main() == 0
    assert not storage.needs_upgrade(settings.db_path)
    assert storage.main() == 0
    assert "already uses the compressed storage format" in caplog.text
    monkeypatch.setattr(settings, "db_path", tmp_path / "missing.duckdb")
    assert storage.main() == 1


# ── Backups ──────────────────────────────────────────────────────────────────


async def test_backup_copies_the_database(client, settings, tmp_path):
    await client.post("/api/tenants", json=FAKE_TENANT)
    path = write_log(tmp_path / "a.log", [log_line(message="kept in the backup")])
    await importer.import_log_file(app_db(), "fake", "trace", path, "a.log", 0)
    res = await client.post("/api/db/backup")
    assert res.status_code == 201, res.text
    body = res.json()
    dest = settings.db_path.parent / "backups" / os.path.basename(body["path"])
    assert body["path"] == str(dest)
    assert body["size_bytes"] == dest.stat().st_size
    with duckdb.connect(str(dest), read_only=True) as conn:
        assert conn.execute("SELECT message FROM logs").fetchall() == [("kept in the backup",)]
        assert conn.execute("SELECT id FROM tenants").fetchall() == [("fake",)]
        assert storage.storage_version(conn) == "v1.5.0+"
        assert migrations.current_version(conn) == migrations.discover()[-1].version
    listed = (await client.get("/api/db/backups")).json()
    assert [b["name"] for b in listed] == [dest.name]
    assert listed[0]["size_bytes"] == dest.stat().st_size
    # The app keeps working after the backup.
    assert (await client.get("/api/logs")).json()["total"] == 1


async def test_backup_goes_to_backup_dir(client, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "elsewhere")
    res = await client.post("/api/db/backup")
    assert res.status_code == 201
    assert res.json()["path"].startswith(str(tmp_path / "elsewhere"))


async def test_backup_is_refused_while_a_fetch_runs(client, fake_cpi):
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(1))
    fake_cpi.download_delay = 0.3
    await client.post("/api/fetch", json={"tenants": ["fake"], "log_types": ["trace"], "hours": 0})
    res = await client.post("/api/db/backup")
    assert res.status_code == 409
    await wait_for_job(client)


async def test_backup_needs_free_disk_space(client, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**9, 10**9, 10))
    res = await client.post("/api/db/backup")
    assert res.status_code == 507
    assert "free disk space" in res.json()["detail"]


async def test_no_backups_yet(client):
    assert (await client.get("/api/db/backups")).json() == []


def test_backup_keeps_the_storage_version_of_old_files(tmp_path):
    path = tmp_path / "old.duckdb"
    make_old_format_db(path, rows=10)
    with duckdb.connect(str(path)) as conn:
        storage.backup_to(conn, tmp_path / "copy.duckdb")
    with duckdb.connect(str(tmp_path / "copy.duckdb"), read_only=True) as conn:
        assert storage.storage_version(conn) == "v1.0.0+"
        assert counts(conn)["logs"] == 11
    assert not (tmp_path / "copy.duckdb.partial").exists()


async def test_concurrent_reads_continue_during_a_backup(client):
    results = await asyncio.gather(client.post("/api/db/backup"), client.get("/api/tenants"), client.get("/api/stats"))
    assert [r.status_code for r in results] == [201, 200, 200]
