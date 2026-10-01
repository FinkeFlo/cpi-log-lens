"""Health endpoints, statistics, database info/clear/cleanup and the database's busy handling."""

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from app import migrations
from app.repositories import database
from app.repositories import logs as logs_repo
from app.services import importer, stats
from tests.support import app_db, log_line, numbered_lines, write_log

pytestmark = pytest.mark.anyio

TENANT = {
    "id": "t1",
    "name": "T1",
    "api_url": "https://x.example",
    "oauth_url": "https://x.example/t",
    "client_id": "c",
    "client_secret": "s",
}


async def import_lines(tmp_path, tenant, lines, name="a.log"):
    db = app_db()
    path = write_log(tmp_path / f"{tenant}-{name}", lines)
    await importer.import_log_file(db, tenant, "trace", path, name, 0)


def ts(delta: timedelta) -> str:
    return (datetime.now() - delta).strftime("%Y-%m-%d %H:%M:%S")


async def test_healthz(client, settings):
    body = (await client.get("/healthz")).json()
    assert body["ok"] is True
    assert body["version"] == settings.app_version
    assert isinstance(body["loop_lag_s"], float)


async def test_readyz(client):
    assert (await client.get("/readyz")).json() == {"ok": True, "fetch_job": "idle"}


async def test_db_info(client, tmp_path, settings):
    await import_lines(tmp_path, "t1", numbered_lines(3))
    await client.post("/api/tenants", json=TENANT)
    body = (await client.get("/api/db/info")).json()
    assert body["path"] == str(settings.db_path)
    assert body["entries"] == 3
    assert body["tenants"] == 1
    assert body["size_bytes"] > 0
    assert body["size_mb"] == round(body["size_bytes"] / 1024 / 1024, 1)
    assert body["schema_version"] == migrations.discover()[-1].version


async def test_clear_deletes_logs_and_bookkeeping_but_keeps_tenants(client, tmp_path):
    await client.post("/api/tenants", json=TENANT)
    await import_lines(tmp_path, "t1", numbered_lines(3))
    assert (await client.post("/api/db/clear")).json() == {"ok": True}
    db = app_db()
    assert await db.read(db.fetch_val, "SELECT count(*) FROM logs") == 0
    assert await db.read(db.fetch_val, "SELECT count(*) FROM file_imports") == 0
    assert len((await client.get("/api/tenants")).json()) == 1
    assert (await client.get("/api/stats")).json()["total"] == 0


async def test_clear_also_deletes_unparsed_lines(client, tmp_path):
    await import_lines(tmp_path, "t1", ["garbage", *numbered_lines(1)])
    await client.post("/api/db/clear")
    db = app_db()
    assert await db.read(db.fetch_val, "SELECT count(*) FROM unparsed_lines") == 0


async def test_cleanup_deletes_entries_older_than_n_days(client, tmp_path):
    await import_lines(
        tmp_path,
        "t1",
        [
            log_line(ts=ts(timedelta(days=40)), message="old"),
            log_line(ts=ts(timedelta(days=10)), message="recent"),
        ],
    )
    await import_lines(tmp_path, "t2", [log_line(ts=ts(timedelta(days=40)), message="old t2")])
    res = await client.post("/api/db/cleanup", json={"older_than_days": 30, "tenant": "t1"})
    assert res.json() == {"ok": True, "deleted": 1, "remaining": 2}
    res = await client.post("/api/db/cleanup", json={"older_than_days": 30})
    assert res.json() == {"ok": True, "deleted": 1, "remaining": 1}
    items = (await client.get("/api/logs")).json()["items"]
    assert [i["message"] for i in items] == ["recent"]


async def test_stats(client, tmp_path):
    await import_lines(
        tmp_path,
        "t1",
        [
            log_line(ts=ts(timedelta(hours=1)), level="ERROR", thread="1-Flow_A_Worker-1"),
            log_line(ts=ts(timedelta(hours=1)), level="error", thread="1-Flow_A_Worker-1"),
            log_line(ts=ts(timedelta(days=5)), level="ERROR", thread="1-Flow_B_Worker-1"),
            log_line(ts=ts(timedelta(hours=2)), level="INFO", thread="1-Flow_B_Worker-1"),
        ],
    )
    await import_lines(tmp_path, "t2", [log_line(level="WARN")])
    body = (await client.get("/api/stats")).json()
    assert set(body) == {"total", "levels", "top_errors", "timeline", "per_tenant"}
    assert body["total"] == 5
    # Sorted by count; level names are upper-cased.
    assert body["levels"][0] == {"lvl": "ERROR", "cnt": 3}
    assert sorted(body["levels"][1:], key=lambda r: r["lvl"]) == [{"lvl": "INFO", "cnt": 1}, {"lvl": "WARN", "cnt": 1}]
    assert body["top_errors"] == [{"iflow": "Flow_A", "cnt": 2}, {"iflow": "Flow_B", "cnt": 1}]
    # Errors per hour, last 48 hours only.
    assert sum(r["cnt"] for r in body["timeline"]) == 2
    assert [(r["tenant"], r["log_type"], r["cnt"]) for r in body["per_tenant"]] == [
        ("t1", "trace", 4),
        ("t2", "trace", 1),
    ]
    t2 = (await client.get("/api/stats", params={"tenant": "t2"})).json()
    assert t2["total"] == 1
    assert t2["top_errors"] == []


async def test_stats_are_cached_until_data_changes(client, tmp_path, monkeypatch):
    calls = []
    real = logs_repo.compute_stats

    async def counting(db, tenant=None):
        calls.append(tenant)
        return await real(db, tenant)

    monkeypatch.setattr(logs_repo, "compute_stats", counting)
    await client.get("/api/stats")
    await client.get("/api/stats")
    assert len(calls) == 1
    await import_lines(tmp_path, "t1", numbered_lines(1))
    assert (await client.get("/api/stats")).json()["total"] == 1
    assert len(calls) == 2


# ── Busy database: bounded waits instead of hanging requests ────────────────

SLOW_QUERY = "SELECT count(*) FROM range(100000000000) t(i) WHERE i % 7 = 3"


async def test_slow_read_is_interrupted_after_the_query_timeout(db, monkeypatch, settings):
    monkeypatch.setattr(settings, "query_timeout_s", 0.2)
    t0 = time.perf_counter()
    with pytest.raises(database.DBBusyError, match=r"longer than 0\.2s"):
        await db.read(db.fetch_val, SLOW_QUERY)
    assert time.perf_counter() - t0 < 2
    # The interrupted cursor goes back to the pool and works again.
    assert await db.read(db.fetch_val, "SELECT 42") == 42


async def test_read_fails_fast_when_all_cursors_are_busy(db, monkeypatch, settings):
    monkeypatch.setattr(settings, "query_timeout_s", 1.0)
    monkeypatch.setattr(settings, "pool_acquire_timeout_s", 0.1)
    slow = [asyncio.create_task(db.read(db.fetch_val, SLOW_QUERY)) for _ in range(database.READ_POOL_SIZE)]
    await asyncio.sleep(0.05)
    with pytest.raises(database.DBBusyError, match="no free read connection"):
        await db.read(db.fetch_val, "SELECT 1")
    results = await asyncio.gather(*slow, return_exceptions=True)
    assert all(isinstance(r, database.DBBusyError) for r in results)


async def test_write_gives_up_waiting_for_the_writer(db, monkeypatch, settings):
    monkeypatch.setattr(settings, "write_lock_timeout_s", 0.1)
    slow_write = asyncio.create_task(db.run(lambda conn: time.sleep(0.5)))
    await asyncio.sleep(0.05)
    with pytest.raises(database.DBBusyError, match="another write"):
        await db.run(lambda conn: None)
    await slow_write


async def test_busy_database_answers_503_with_retry_after(client, monkeypatch):
    async def busy(*args, **kwargs):
        raise database.DBBusyError("database busy: no free read connection")

    monkeypatch.setattr(stats, "get_stats", busy)
    res = await client.get("/api/stats")
    assert res.status_code == 503
    assert res.headers["retry-after"] == "5"
    assert res.json() == {"detail": "database busy: no free read connection"}
