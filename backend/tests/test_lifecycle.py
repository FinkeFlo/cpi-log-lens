"""Start and clean shutdown: background work stops, the database is checkpointed and closed."""

import asyncio
import logging
import threading
import time

import duckdb
import httpx
import pytest
from asgi_lifespan import LifespanManager

from app import main, tasks, watchdog
from app.services import fetch as fetch_service
from tests.support import FAKE_TENANT, numbered_lines, wait_for_job

pytestmark = pytest.mark.anyio

SLOW_READ = "SELECT count(*) AS n FROM range(100000000000) t(i) WHERE i % 7 = 3"
SLOW_WRITE = f"CREATE TABLE slow AS {SLOW_READ}"


async def test_shutdown_checkpoints_and_closes_the_database(app_env, settings, caplog):
    caplog.set_level(logging.INFO)
    async with (
        LifespanManager(main.app) as manager,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url="http://localhost") as client,
    ):
        assert (await client.post("/api/demo")).status_code == 200
        await wait_for_job(client)
    assert main.app.state.db.closed
    assert tasks.background_tasks == set()
    assert "database checkpointed and closed" in caplog.text
    # Nothing is left in the WAL, and the file is no longer locked.
    assert not settings.db_path.with_name(settings.db_path.name + ".wal").exists()
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM logs").fetchall() == [(1803,)]


async def test_shutdown_cancels_a_running_fetch(app_env, fake_cpi):
    fake_cpi.add("a.log", numbered_lines(2))
    fake_cpi.download_delay = 30
    async with (
        LifespanManager(main.app) as manager,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url="http://localhost") as client,
    ):
        await client.post("/api/tenants", json=FAKE_TENANT)
        await client.post("/api/fetch", json={"tenants": ["fake"], "log_types": ["trace"], "hours": 0})
        await asyncio.sleep(0.1)
        assert (await client.get("/api/fetch/status")).json()["status"] == "running"
        t0 = time.perf_counter()
    assert time.perf_counter() - t0 < 5
    assert fetch_service.active_job is not None
    assert fetch_service.active_job.status == "cancelled"
    assert main.app.state.db.closed


async def test_close_waits_for_a_running_write(db):
    def slow_write(conn):
        time.sleep(0.3)
        conn.execute("CREATE TABLE t AS SELECT 1 AS x")

    write = asyncio.create_task(db.run(slow_write))
    await asyncio.sleep(0.05)
    await db.close()
    await write
    assert db.closed


async def test_close_interrupts_a_write_that_takes_too_long(db, settings, caplog):
    write = asyncio.create_task(db.run(lambda conn: conn.execute(SLOW_WRITE)))
    await asyncio.sleep(0.1)
    t0 = time.perf_counter()
    await db.close(write_wait_s=0.2)
    assert time.perf_counter() - t0 < 5
    assert "interrupting the running write" in caplog.text
    with pytest.raises(duckdb.InterruptException):
        await write
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'slow'").fetchall() == [(0,)]


async def test_close_interrupts_running_reads(db, settings, monkeypatch):
    monkeypatch.setattr(settings, "query_timeout_s", 60)
    read = asyncio.create_task(db.read(db.fetch_val, SLOW_READ))
    await asyncio.sleep(0.1)
    t0 = time.perf_counter()
    await db.close()
    assert time.perf_counter() - t0 < 5
    with pytest.raises(duckdb.InterruptException):
        await read


def test_watchdog_thread_ends_when_stopped():
    stop = threading.Event()
    thread = threading.Thread(target=watchdog.watchdog, args=(stop, 120), daemon=True)
    thread.start()
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
