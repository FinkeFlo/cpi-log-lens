"""Fetch jobs: start, cancel, progress (status and SSE), demo data and the saved form defaults."""

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.deps import DbDep, FetchDep
from app.api.schemas import DefaultFetchConfig, FetchRequest
from app.repositories import app_settings as settings_repo
from app.repositories import fetch_runs as fetch_runs_repo
from app.services import fetch as fetch_service
from app.services import tenants as tenant_service
from app.services.fetch import FetchService

router = APIRouter(prefix="/api", tags=["fetch"])


async def _start(fetch: FetchService, body: FetchRequest, trigger: str) -> dict:
    # A running job: JobAlreadyRunning -> 409 with the running job's id (app/errors.py).
    job = await fetch.start(fetch_service.FetchParams(**body.model_dump()), trigger=trigger)
    return {"ok": True, "job_id": job.id}


@router.post("/demo")
async def start_demo(db: DbDep, fetch: FetchDep):
    """Create the demo tenant (if needed) and import the bundled sample logs."""
    await tenant_service.ensure_demo_tenant(db)
    body = FetchRequest(tenants=[tenant_service.DEMO_TENANT_ID], log_types=["trace"], hours=0)
    return await _start(fetch, body, "demo")


@router.post("/fetch")
async def fetch_logs(body: FetchRequest, fetch: FetchDep):
    """Start a background fetch job. Returns job id immediately."""
    return await _start(fetch, body, "manual")


@router.get("/fetch/status")
async def fetch_status(fetch: FetchDep):
    """Return current snapshot of the active job (for polling or initial state)."""
    job = fetch.job
    if not job:
        return {"status": "idle"}
    return job.snapshot()


@router.post("/fetch/cancel")
async def fetch_cancel(fetch: FetchDep):
    """Request cancellation of the active job. run_fetch() checks
    `cancel_requested` at each file boundary and stops cleanly (finishes the
    file currently in flight rather than being killed mid-write, so the DB
    stays consistent) instead of requiring a full container restart."""
    job = fetch.request_cancel()
    if job is None:
        raise HTTPException(409, "No fetch is running.")
    return {"ok": True, "job_id": job.id}


@router.get("/fetch/runs")
async def fetch_runs(db: DbDep, limit: int = Query(20, ge=1, le=200)):
    """History of fetch jobs, newest first: trigger (manual, demo, schedule:<id>),
    parameters, status (running, done, error, cancelled, interrupted), times and counters."""
    return await fetch_runs_repo.list_runs(db, limit)


@router.get("/fetch/runs/{run_id}")
async def fetch_run(run_id: str, db: DbDep):
    run = await fetch_runs_repo.get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Fetch run not found")
    return run


@router.get("/fetch/stream")
async def fetch_stream(fetch: FetchDep):
    """SSE stream — attach to the active job from any page/tab."""
    return StreamingResponse(
        fetch.event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/fetch/default-config")
async def get_default_fetch_config(db: DbDep):
    """Return the saved default fetch form config (tenants/log_types/hours),
    or null if none has been saved yet — the frontend falls back to
    'all tenants + all log types' in that case."""
    raw = await settings_repo.get_setting(db, "default_fetch_config")
    return json.loads(raw) if raw else None


@router.put("/fetch/default-config")
async def set_default_fetch_config(body: DefaultFetchConfig, db: DbDep):
    """Persist the current fetch form selection as the default shown on
    next page load, instead of always defaulting to all tenants/log types."""
    await settings_repo.set_setting(db, "default_fetch_config", body.model_dump_json())
    return {"ok": True}
