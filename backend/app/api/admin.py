"""Database administration: size and entry count, clear, delete old entries."""

from fastapi import APIRouter

from app.api.deps import DbDep
from app.api.schemas import CleanupRequest
from app.config import get_settings
from app.repositories import logs as logs_repo
from app.services import stats

router = APIRouter(prefix="/api/db", tags=["admin"])


@router.get("/info")
async def db_info(db: DbDep):
    return await logs_repo.get_db_info(db, get_settings().db_path)


@router.post("/clear")
async def db_clear(db: DbDep):
    await logs_repo.clear_db(db)
    stats.invalidate()
    return {"ok": True}


@router.post("/cleanup")
async def db_cleanup(req: CleanupRequest, db: DbDep):
    """Delete log entries older than N days, optionally filtered by tenant."""
    result = await logs_repo.cleanup_old_logs(db, req.older_than_days, req.tenant)
    stats.invalidate()
    return {"ok": True, **result}
