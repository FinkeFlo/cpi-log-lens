"""Fetch schedules: CRUD API, when the scheduler starts a fetch, and retention."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app import main, tasks
from app.config import get_settings
from app.repositories import logs as logs_repo
from app.repositories import schedules as schedules_repo
from app.services import fetch as fetch_service
from app.services import scheduler

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


# ── When a schedule is due (pure function) ───────────────────────────────────

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("last_run_at", "expected"),
    [
        (None, NOW),  # never ran: right away
        ("2026-01-15 11:50:00+00", NOW + timedelta(minutes=5)),  # 10 of 15 minutes passed
        ("2026-01-15 11:45:00+00", NOW),  # exactly due
        ("2026-01-15 09:00:00+00", NOW),  # overdue: right away, not several times
        ("2026-01-15 13:50:00+02", NOW + timedelta(minutes=5)),  # other UTC offset
        ("2026-01-15 11:50:00", NOW + timedelta(minutes=5)),  # no offset: UTC
    ],
)
def test_next_run(last_run_at, expected):
    assert scheduler.next_run(last_run_at, 15, NOW) == expected


# ── Scheduler with APScheduler (one "minute" = 20 ms) ────────────────────────


@pytest.fixture
def fast_minutes(monkeypatch):
    monkeypatch.setattr(scheduler, "MINUTE", 0.02)


async def add_schedule(db, sid, *, enabled=True, interval=15, last_run_sql=None):
    await schedules_repo.create_schedule(db, sid, sid, '["all"]', '["trace"]', 2, interval, enabled)
    if last_run_sql:
        await db.run(db.execute, f"UPDATE fetch_schedules SET last_run_at = {last_run_sql} WHERE id = ?", [sid])


async def last_run(db, sid):
    schedule = await schedules_repo.get_schedule(db, sid)
    assert schedule is not None
    return schedule["last_run_at"]


class StubRuns:
    """Stands in for the fetch: records what was started, ends after `duration` with `status`."""

    def __init__(self, status="done", duration=0.0):
        self.started: list = []
        self.status = status
        self.duration = duration

    async def __call__(self, db, job, params):
        self.started.append((job.trigger, params))
        await asyncio.sleep(self.duration)
        await job.finish(self.status, {"type": self.status})


async def run_service(db, runner, *, seconds, running_job=None, before=None):
    fetch = fetch_service.FetchService(db, runner=runner)
    fetch.job = running_job
    service = scheduler.ScheduleService(db, fetch)
    await service.start()
    try:
        if before is not None:
            await before(fetch)
        await asyncio.sleep(seconds)
    finally:
        service.shutdown()
        await asyncio.gather(*tasks.background_tasks, return_exceptions=True)
    return service


async def test_never_run_schedule_starts_right_away(db, fast_minutes):
    await add_schedule(db, "s1", interval=100)
    runs = StubRuns()
    await run_service(db, runs, seconds=0.2)
    assert [t for t, _ in runs.started] == ["schedule:s1"]
    params = runs.started[0][1]
    assert (params.tenants, params.log_types, params.hours) == (["all"], ["trace"], 2)
    # last_run_at is text with the local UTC offset, e.g. "2026-01-15 09:00:00.123+01".
    assert datetime.fromisoformat(await last_run(db, "s1")).tzinfo is not None


async def test_schedule_runs_again_every_interval(db, fast_minutes):
    await add_schedule(db, "s1", interval=5)  # 100 ms
    runs = StubRuns()
    await run_service(db, runs, seconds=0.45)
    assert 3 <= len(runs.started) <= 6


async def test_schedule_within_its_interval_waits(db, fast_minutes):
    await add_schedule(db, "s1", last_run_sql="CURRENT_TIMESTAMP")  # due in 15 "minutes" = 300 ms
    runs = StubRuns()
    await run_service(db, runs, seconds=0.15)
    assert runs.started == []


async def test_disabled_schedule_never_runs(db, fast_minutes):
    await add_schedule(db, "s1", enabled=False)
    runs = StubRuns()
    service = await run_service(db, runs, seconds=0.15)
    assert runs.started == []
    assert service.job_ids() == []


async def test_due_schedule_retries_while_another_fetch_runs(db, fast_minutes):
    await add_schedule(db, "s1", interval=100)
    manual = fetch_service.FetchJob(id="manual", status="running")
    runs = StubRuns()

    async def end_manual_job_later(fetch):
        await asyncio.sleep(0.1)
        manual.status = "done"

    await run_service(db, runs, seconds=0.3, running_job=manual, before=end_manual_job_later)
    # Not lost: started once the manual job had ended (retry after one "minute").
    assert [t for t, _ in runs.started] == ["schedule:s1"]


async def test_several_due_schedules_all_run_one_after_another(db, fast_minutes):
    for sid in ("s1", "s2", "s3"):
        await add_schedule(db, sid, interval=1000)
    runs = StubRuns(duration=0.02)
    await run_service(db, runs, seconds=0.4)
    assert sorted(t for t, _ in runs.started) == ["schedule:s1", "schedule:s2", "schedule:s3"]
    for sid in ("s1", "s2", "s3"):
        assert await last_run(db, sid) is not None


async def test_failed_run_does_not_count_as_last_run(db, fast_minutes):
    await add_schedule(db, "s1", interval=1000)
    runs = StubRuns(status="error")
    await run_service(db, runs, seconds=0.15)
    assert len(runs.started) == 1
    assert await last_run(db, "s1") is None


async def test_schedule_changes_reach_the_scheduler(client):
    service = main.app.state.schedules
    sid = (await client.post("/api/schedules", json=SCHEDULE)).json()["id"]
    assert service.job_ids() == [f"schedule:{sid}"]
    await client.put(f"/api/schedules/{sid}", json={**SCHEDULE, "enabled": False})
    assert service.job_ids() == []
    await client.put(f"/api/schedules/{sid}", json=SCHEDULE)
    assert service.job_ids() == [f"schedule:{sid}"]
    await client.delete(f"/api/schedules/{sid}")
    assert service.job_ids() == []


# ── Retention ────────────────────────────────────────────────────────────────


@pytest.fixture
def retention(monkeypatch):
    calls = []

    async def fake_cleanup(db, days, tenant=None):
        calls.append(days)
        return {"deleted": 0, "remaining": 0}

    monkeypatch.setattr(logs_repo, "cleanup_old_logs", fake_cleanup)
    monkeypatch.setattr(get_settings(), "retention_days", 30)
    return calls


async def test_retention_runs_at_start_and_then_every_interval(db, monkeypatch, retention):
    monkeypatch.setattr(get_settings(), "retention_check_hours", 0.05 / 3600)
    await run_service(db, StubRuns(), seconds=0.4)
    assert len(retention) >= 3
    assert set(retention) == {30}


async def test_retention_is_off_without_retention_days(db, monkeypatch, retention):
    monkeypatch.setattr(get_settings(), "retention_days", 0)
    service = await run_service(db, StubRuns(), seconds=0.1)
    assert retention == []
    assert service.job_ids() == []


async def test_retention_retries_soon_after_a_running_fetch(db, monkeypatch, fast_minutes, retention):
    monkeypatch.setattr(get_settings(), "retention_check_hours", 1)
    job = fetch_service.FetchJob(id="j", status="running")

    async def end_job_later(fetch):
        await asyncio.sleep(0.05)
        assert retention == []  # skipped while the fetch runs
        job.status = "done"

    # Retry after 5 "minutes" (100 ms), not after the hour-long interval.
    await run_service(db, StubRuns(), seconds=0.3, running_job=job, before=end_job_later)
    assert retention == [30]
