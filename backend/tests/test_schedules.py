"""Fetch schedules: CRUD API and when the scheduler starts a fetch."""

import asyncio
from datetime import datetime

import pytest

import db as database
import main

pytestmark = pytest.mark.anyio

SCHEDULE = {"name": "every 15 min", "tenants": ["all"], "log_types": ["trace"], "hours": 1, "interval_minutes": 15}


async def test_create_list_update_delete(client):
    res = await client.post("/api/schedules", json=SCHEDULE)
    assert res.status_code == 201
    sid = res.json()["id"]
    assert res.json() == {"ok": True, "id": sid}
    listed = (await client.get("/api/schedules")).json()
    assert len(listed) == 1
    s = listed[0]
    assert {k: s[k] for k in SCHEDULE} == SCHEDULE
    assert s["enabled"] is True
    assert s["last_run_at"] is None
    changed = {**SCHEDULE, "name": "hourly", "tenants": ["dev", "qa"], "interval_minutes": 60, "enabled": False}
    assert (await client.put(f"/api/schedules/{sid}", json=changed)).json() == {"ok": True}
    s = (await client.get("/api/schedules")).json()[0]
    assert {k: s[k] for k in changed} == changed
    assert (await client.delete(f"/api/schedules/{sid}")).json() == {"ok": True}
    assert (await client.get("/api/schedules")).json() == []


async def test_schedule_defaults(client):
    await client.post("/api/schedules", json={"name": "defaults"})
    s = (await client.get("/api/schedules")).json()[0]
    assert (s["tenants"], s["log_types"], s["hours"], s["interval_minutes"], s["enabled"]) == (
        ["all"],
        ["trace", "http"],
        1,
        15,
        True,
    )


async def test_update_of_an_unknown_schedule_is_404(client):
    res = await client.put("/api/schedules/nope", json=SCHEDULE)
    assert res.status_code == 404
    assert res.json() == {"detail": "Schedule not found"}


# ── Due logic of the scheduler loop ──────────────────────────────────────────


async def add_schedule(db, sid, *, enabled=True, interval=15, last_run_sql=None):
    await database.create_schedule(db, sid, sid, '["all"]', '["trace"]', 2, interval, enabled)
    if last_run_sql:
        await db.run(db._execute, f"UPDATE fetch_schedules SET last_run_at = {last_run_sql} WHERE id = ?", [sid])


async def run_scheduler(monkeypatch, *, seconds=0.3, job_status="done"):
    """Run the scheduler loop with a fast tick and a stub fetch; returns the started requests."""
    started = []

    async def fake_run_fetch(job, body):
        started.append(body)
        job.status = job_status

    monkeypatch.setattr(main, "_run_fetch", fake_run_fetch)
    monkeypatch.setattr(main, "SCHEDULE_CHECK_SECONDS", 0.02)
    task = asyncio.create_task(main._schedule_loop())
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.gather(*main._background_tasks, return_exceptions=True)
    return started


async def last_run(db, sid):
    schedule = await database.get_schedule(db, sid)
    assert schedule is not None
    return schedule["last_run_at"]


async def test_never_run_schedule_is_due(db, monkeypatch):
    await add_schedule(db, "s1")
    started = await run_scheduler(monkeypatch)
    assert len(started) == 1
    assert (started[0].tenants, started[0].log_types, started[0].hours) == (["all"], ["trace"], 2)
    # last_run_at is text with the local UTC offset, e.g. "2026-01-15 09:00:00.123+01".
    assert datetime.fromisoformat(await last_run(db, "s1")).tzinfo is not None


async def test_schedule_run_within_its_interval_is_not_due(db, monkeypatch):
    await add_schedule(db, "s1", last_run_sql="CURRENT_TIMESTAMP - INTERVAL 14 MINUTE")
    assert await run_scheduler(monkeypatch) == []


async def test_schedule_is_due_once_its_interval_has_passed(db, monkeypatch):
    await add_schedule(db, "s1", last_run_sql="CURRENT_TIMESTAMP - INTERVAL 16 MINUTE")
    assert len(await run_scheduler(monkeypatch)) == 1


async def test_disabled_schedule_never_runs(db, monkeypatch):
    await add_schedule(db, "s1", enabled=False)
    assert await run_scheduler(monkeypatch) == []


async def test_due_schedule_waits_while_a_fetch_is_running(db, monkeypatch):
    await add_schedule(db, "s1")
    monkeypatch.setattr(main, "_active_job", main.FetchJob(id="manual", status="running"))
    assert await run_scheduler(monkeypatch) == []
    assert await last_run(db, "s1") is None


async def test_several_due_schedules_all_run_one_after_another(db, monkeypatch):
    for sid in ("s1", "s2", "s3"):
        await add_schedule(db, sid)
    started = await run_scheduler(monkeypatch)
    assert len(started) == 3
    for sid in ("s1", "s2", "s3"):
        assert await last_run(db, sid) is not None


@pytest.mark.xfail(reason="ARC-13: last_run_at is set before the fetch runs, a failed run counts as done")
async def test_failed_run_does_not_count_as_last_run(db, monkeypatch):
    await add_schedule(db, "s1")
    await run_scheduler(monkeypatch, job_status="error")
    assert await last_run(db, "s1") is None


# ── Retention loop ───────────────────────────────────────────────────────────


async def run_retention(monkeypatch, *, check_hours, seconds, job=None, finish_job_after=None):
    calls = []

    async def fake_cleanup(db, days, tenant=None):
        calls.append(days)
        return {"deleted": 0, "remaining": 0}

    monkeypatch.setattr(database, "cleanup_old_logs", fake_cleanup)
    monkeypatch.setattr(main, "RETENTION_DAYS", 30)
    monkeypatch.setattr(main, "RETENTION_CHECK_HOURS", check_hours)
    monkeypatch.setattr(main, "_active_job", job)
    task = asyncio.create_task(main._retention_loop())
    if finish_job_after is not None:
        await asyncio.sleep(finish_job_after)
        job.status = "done"
        await asyncio.sleep(seconds - finish_job_after)
    else:
        await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return calls


async def test_retention_runs_at_start_and_then_every_interval(db, monkeypatch):
    calls = await run_retention(monkeypatch, check_hours=0.05 / 3600, seconds=0.4)
    assert len(calls) >= 3
    assert set(calls) == {30}


async def test_retention_skips_while_a_fetch_is_running(db, monkeypatch):
    job = main.FetchJob(id="j", status="running")
    assert await run_retention(monkeypatch, check_hours=0.1 / 3600, seconds=0.25, job=job) == []


@pytest.mark.xfail(reason="RES-12: with a fetch running, retention waits a whole interval instead of retrying soon")
async def test_retention_retries_soon_after_the_fetch_finished(db, monkeypatch):
    job = main.FetchJob(id="j", status="running")
    calls = await run_retention(monkeypatch, check_hours=1, seconds=1.0, job=job, finish_job_after=0.1)
    assert calls == [30]
