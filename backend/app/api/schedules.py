"""Recurring fetch schedules."""

import json
import uuid

from fastapi import APIRouter, HTTPException

from app.api.deps import DbDep
from app.api.schemas import ScheduleRequest
from app.repositories import schedules as schedules_repo

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


@router.get("")
async def list_schedules(db: DbDep):
    schedules = await schedules_repo.get_schedules(db)
    for s in schedules:
        s["tenants"] = json.loads(s["tenants"])
        s["log_types"] = json.loads(s["log_types"])
    return schedules


@router.post("", status_code=201)
async def create_schedule(body: ScheduleRequest, db: DbDep):
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
    return {"ok": True, "id": schedule_id}


@router.put("/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleRequest, db: DbDep):
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
    return {"ok": True}


@router.delete("/{schedule_id}")
async def remove_schedule(schedule_id: str, db: DbDep):
    await schedules_repo.delete_schedule(db, schedule_id)
    return {"ok": True}
