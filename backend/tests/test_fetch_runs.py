"""Fetch job service: one job at a time (also under concurrent starts) and the
persisted run history (GET /api/fetch/runs)."""

import asyncio
from datetime import datetime

import duckdb
import httpx
import pytest
from asgi_lifespan import LifespanManager

from app import main
from app.repositories import fetch_runs as fetch_runs_repo
from app.services import fetch as fetch_service
from tests.support import FAKE_TENANT, app_db, fetch, numbered_lines, wait_for_job

pytestmark = pytest.mark.anyio

TRACE = {"tenants": ["fake"], "log_types": ["trace"], "hours": 0}


async def runs(client, **params):
    res = await client.get("/api/fetch/runs", params=params)
    assert res.status_code == 200, res.text
    return res.json()


async def test_a_finished_run_is_recorded(client, fake_cpi):
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(3))
    fake_cpi.add("b.log", numbered_lines(2))
    fake_cpi.download_status["b.log"] = [404]
    status = await fetch(client, **TRACE)
    [run] = await runs(client)
    assert run["id"] == status["job_id"]
    assert run["trigger"] == "manual"
    assert run["params"] == {"tenants": ["fake"], "log_types": ["trace"], "hours": 0}
    assert run["status"] == "done"
    assert (run["files_total"], run["files_done"], run["rows_imported"]) == (2, 2, 3)
    assert (run["warnings"], run["errors"], run["error"]) == (1, 0, None)
    started, finished = datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["finished_at"])
    assert started.tzinfo is not None
    assert started <= finished
    assert (await client.get(f"/api/fetch/runs/{run['id']}")).json() == run


async def test_failed_cancelled_and_demo_runs(client, fake_cpi, monkeypatch):
    monkeypatch.setattr(fetch_service, "PROGRESS_SAVE_SECONDS", 3600)
    status = await fetch(client, tenants=["nope"], log_types=["trace"], hours=0)
    assert status["status"] == "error"

    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.token_status = 401
    await fetch(client, **TRACE)

    fake_cpi.token_status = 200
    for i in range(3):
        fake_cpi.add(f"f{i}.log", numbered_lines(1))
    fake_cpi.download_delay = 0.2
    await client.post("/api/fetch", json=TRACE)
    await asyncio.sleep(0.1)
    await client.post("/api/fetch/cancel")
    await wait_for_job(client)

    await client.post("/api/demo")
    await wait_for_job(client)

    history = await runs(client)
    assert [(r["trigger"], r["status"]) for r in history] == [
        ("demo", "done"),
        ("manual", "cancelled"),
        ("manual", "done"),
        ("manual", "error"),
    ]
    assert history[0]["rows_imported"] == 1803
    assert history[2]["errors"] == 1
    assert "OAuth token" in history[2]["error"]
    assert history[3]["error"] == "No tenants configured."


async def test_concurrent_starts_run_one_job(client, fake_cpi):
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(1))
    fake_cpi.download_delay = 0.3
    answers = await asyncio.gather(*[client.post("/api/fetch", json=TRACE) for _ in range(5)])
    started = [a.json() for a in answers if a.status_code == 200]
    refused = [a.json() for a in answers if a.status_code == 409]
    assert len(started) == 1
    assert len(refused) == 4
    assert {r["job_id"] for r in refused} == {started[0]["job_id"]}
    await wait_for_job(client)
    assert len(await runs(client)) == 1


async def test_service_start_is_guarded_by_a_lock(db):
    release = asyncio.Event()

    async def slow_runner(db, job, params):
        await release.wait()
        job.status = "done"

    service = fetch_service.FetchService(db, runner=slow_runner)
    params = fetch_service.FetchParams(["all"], ["trace"], 1)
    results = await asyncio.gather(*[service.start(params, trigger=f"t{i}") for i in range(10)], return_exceptions=True)
    jobs = [r for r in results if isinstance(r, fetch_service.FetchJob)]
    assert len(jobs) == 1
    assert all(isinstance(r, fetch_service.JobAlreadyRunning) for r in results if r not in jobs)
    release.set()
    await asyncio.sleep(0.05)
    assert [r["trigger"] for r in await fetch_runs_repo.list_runs(db, 10)] == [jobs[0].trigger]


async def test_progress_is_saved_while_the_job_runs(db, monkeypatch):
    monkeypatch.setattr(fetch_service, "PROGRESS_SAVE_SECONDS", 0.05)
    release = asyncio.Event()

    async def runner(db, job, params):
        job.push({"type": "files_found", "count": 4, "tenant": "t", "log_type": "trace"})
        job.push({"type": "progress", "done": 1, "total": 4, "file": "a", "new_rows": 7, "imported": 7})
        job.imported = 7
        await release.wait()
        job.status = "done"

    service = fetch_service.FetchService(db, runner=runner)
    job = await service.start(fetch_service.FetchParams(["all"], ["trace"], 1))
    await asyncio.sleep(0.2)
    run = await fetch_runs_repo.get_run(db, job.id)
    assert run is not None
    assert (run["status"], run["files_total"], run["files_done"], run["rows_imported"]) == ("running", 4, 1, 7)
    release.set()
    await asyncio.sleep(0.05)
    run = await fetch_runs_repo.get_run(db, job.id)
    assert run is not None
    assert run["status"] == "done"
    assert run["finished_at"] is not None


async def test_runs_left_running_are_marked_interrupted_on_start(app_env, settings):
    async with LifespanManager(main.app):
        db = app_db()
        await fetch_runs_repo.insert_run(db, "old", "manual", {"tenants": ["all"]}, datetime.now().astimezone())
    # (The app's own shutdown found no running job of its own.)
    with duckdb.connect(str(settings.db_path)) as conn:
        conn.execute("UPDATE fetch_runs SET status = 'running', finished_at = NULL WHERE id = 'old'")
    async with (
        LifespanManager(main.app) as manager,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url="http://localhost") as client,
    ):
        [run] = await runs(client)
        assert run["status"] == "interrupted"
        assert run["finished_at"] is not None


async def test_shutdown_records_the_running_job_as_interrupted(app_env, settings, fake_cpi):
    fake_cpi.add("a.log", numbered_lines(1))
    fake_cpi.download_delay = 30
    async with (
        LifespanManager(main.app) as manager,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url="http://localhost") as client,
    ):
        await client.post("/api/tenants", json=FAKE_TENANT)
        await client.post("/api/fetch", json=TRACE)
        await asyncio.sleep(0.1)
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        assert conn.execute("SELECT status, finished_at IS NOT NULL FROM fetch_runs").fetchall() == [
            ("interrupted", True)
        ]


async def test_run_listing(client, monkeypatch):
    monkeypatch.setattr(fetch_runs_repo, "KEEP_RUNS", 3)
    db = app_db()
    for i in range(5):
        await fetch_runs_repo.insert_run(db, f"r{i}", "manual", {"i": i}, datetime(2026, 1, 1, 0, i).astimezone())
    assert [r["id"] for r in await runs(client)] == ["r4", "r3", "r2"]
    assert [r["id"] for r in await runs(client, limit=2)] == ["r4", "r3"]
    assert (await client.get("/api/fetch/runs", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/fetch/runs", params={"limit": 201})).status_code == 422
    assert (await client.get("/api/fetch/runs/r0")).status_code == 404


def test_slow_sse_subscribers_lose_the_oldest_events(monkeypatch):
    monkeypatch.setattr(fetch_service, "SSE_QUEUE_SIZE", 3)
    job = fetch_service.FetchJob(id="j")
    q = job.attach()
    for i in range(5):
        job.push({"type": "status", "msg": str(i)})
    assert [q.get_nowait()["msg"] for _ in range(q.qsize())] == ["2", "3", "4"]


async def test_a_job_shows_as_ended_only_once_its_run_is_recorded(client, fake_cpi, monkeypatch):
    real_update = fetch_runs_repo.update_run

    async def slow_update(db, run_id, **fields):
        await asyncio.sleep(0.2)
        await real_update(db, run_id, **fields)

    monkeypatch.setattr(fetch_runs_repo, "update_run", slow_update)
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(1))
    status = await fetch(client, **TRACE)
    assert status["status"] == "done"
    assert [r["status"] for r in await runs(client)] == ["done"]
