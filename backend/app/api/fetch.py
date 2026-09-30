"""Fetch jobs: start, cancel, progress (status and SSE), demo data and the saved form defaults."""

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import DbDep
from app.api.schemas import DefaultFetchConfig, FetchRequest
from app.repositories import app_settings as settings_repo
from app.repositories.database import Database
from app.services import fetch as fetch_service
from app.services import tenants as tenant_service

router = APIRouter(prefix="/api", tags=["fetch"])


def _start(db: Database, body: FetchRequest) -> dict:
    job = fetch_service.start(db, fetch_service.FetchParams(**body.model_dump()))
    if job is None:
        running = fetch_service.active_job
        assert running is not None
        return {"ok": False, "error": "A fetch is already running.", "job_id": running.id}
    return {"ok": True, "job_id": job.id}


@router.post("/demo")
async def start_demo(db: DbDep):
    """Create the demo tenant (if needed) and import the bundled sample logs."""
    await tenant_service.ensure_demo_tenant(db)
    return _start(db, FetchRequest(tenants=[tenant_service.DEMO_TENANT_ID], log_types=["trace"], hours=0))


@router.post("/fetch")
async def fetch_logs(body: FetchRequest, db: DbDep):
    """Start a background fetch job. Returns job id immediately."""
    return _start(db, body)


@router.get("/fetch/status")
async def fetch_status():
    """Return current snapshot of the active job (for polling or initial state)."""
    job = fetch_service.active_job
    if not job:
        return {"status": "idle"}
    return job.snapshot()


@router.post("/fetch/cancel")
async def fetch_cancel():
    """Request cancellation of the active job. run_fetch() checks
    `cancel_requested` at each file boundary and stops cleanly (finishes the
    file currently in flight rather than being killed mid-write, so the DB
    stays consistent) instead of requiring a full container restart."""
    job = fetch_service.request_cancel()
    if job is None:
        return {"ok": False, "error": "No fetch is running."}
    return {"ok": True, "job_id": job.id}


@router.get("/fetch/stream")
async def fetch_stream():
    """SSE stream — attach to the active job from any page/tab."""
    return StreamingResponse(
        fetch_service.event_stream(),
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
