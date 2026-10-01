"""Liveness and readiness probes."""

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import watchdog
from app.api.deps import DbDep, FetchDep
from app.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    """Liveness: answers as long as the event loop runs; no database access."""
    return {"ok": True, "version": get_settings().app_version, "loop_lag_s": round(watchdog.loop_lag(), 2)}


@router.get("/readyz")
async def readyz(db: DbDep, fetch: FetchDep):
    """Readiness: the database answers a trivial query within 2 s."""
    try:
        await asyncio.wait_for(db.read(db.fetch_val, "SELECT 1"), 2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)
    job = fetch.job
    return {"ok": True, "fetch_job": job.status if job else "idle"}
