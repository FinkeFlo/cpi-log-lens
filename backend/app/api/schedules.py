"""Recurring fetch schedules."""

import json
import uuid

from fastapi import APIRouter, HTTPException

from app.api.deps import DbDep, SchedulesDep
from app.api.schemas import ScheduleRequest
from app.repositories import fetch_runs as fetch_runs_repo
from app.repositories import schedules as schedules_repo

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


# Fields of a schedule's last run in the list.
_RUN_FIELDS = (
    "id",
    "status",
    "started_at",
    "finished_at",
    "files_total",
    "rows_imported",
    "warnings",
    "errors",
    "error",
)


@router.get("")
async def list_schedules(db: DbDep, service: SchedulesDep):
    """The schedules with `next_run_at` (null while disabled) and `last_run`, the newest
    fetch they started (scheduled or "run now", null if none is recorded): status
    (running, done, error, cancelled, interrupted), times, counters and the last error."""
    schedules = await schedules_repo.get_schedules(db)
    next_runs = service.next_runs()
    runs = await fetch_runs_repo.latest_by_trigger(db, [f"schedule:{s['id']}" for s in schedules])
    for s in schedules:
        s["tenants"] = json.loads(s["tenants"])
        s["log_types"] = json.loads(s["log_types"])
        s["next_run_at"] = next_runs.get(s["id"]) if s["enabled"] else None
        run = runs.get(f"schedule:{s['id']}")
        s["last_run"] = {k: run[k] for k in _RUN_FIELDS} if run else None
    return schedules


@router.post("", status_code=201)
async def create_schedule(body: ScheduleRequest, db: DbDep, schedules: SchedulesDep):
    schedule_id = str(uuid.uuid4())
    await schedules_repo.create_schedule(
        db,
        schedule_id,
        body.name,
        json.dumps(body.tenants),
        json.dumps(body.log_types),
        body.hours,
        body.interval_minutes,
        body.enabled,
    )
    await schedules.reload()
    return {"ok": True, "id": schedule_id}


@router.put("/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleRequest, db: DbDep, schedules: SchedulesDep):
    existing = await schedules_repo.get_schedule(db, schedule_id)
    if not existing:
        raise HTTPException(404, "Schedule not found")
    await schedules_repo.update_schedule(
        db,
        schedule_id,
        body.name,
        json.dumps(body.tenants),
        json.dumps(body.log_types),
        body.hours,
        body.interval_minutes,
        body.enabled,
    )
    await schedules.reload()
    return {"ok": True}


@router.post("/{schedule_id}/run")
async def run_schedule(schedule_id: str, schedules: SchedulesDep):
    """Start a schedule's fetch now (also a disabled one), without changing when it runs
    next. 409 with the running job's id if a fetch is already running."""
    job = await schedules.run_now(schedule_id)
    if job is None:
        raise HTTPException(404, "Schedule not found")
    return {"ok": True, "job_id": job.id}


@router.delete("/{schedule_id}")
async def remove_schedule(schedule_id: str, db: DbDep, schedules: SchedulesDep):
    if await schedules_repo.get_schedule(db, schedule_id) is None:
        raise HTTPException(404, "Schedule not found")
    await schedules_repo.delete_schedule(db, schedule_id)
    await schedules.reload()
    return {"ok": True}
