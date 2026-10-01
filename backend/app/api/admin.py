"""Database administration: size and entry count, clear, delete old entries, backups."""

from fastapi import APIRouter, HTTPException

from app.api.deps import DbDep, FetchDep
from app.api.schemas import CleanupRequest
from app.config import get_settings
from app.repositories import logs as logs_repo
from app.services import backup, stats

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


@router.post("/backup", status_code=201)
async def db_backup(db: DbDep, fetch: FetchDep):
    """Write a copy of the database into the backup directory (see README, Backup)."""
    if fetch.is_running():
        raise HTTPException(409, "A fetch is running; start the backup when it has finished.")
    return await backup.create_backup(db)


@router.get("/backups")
async def db_backups():
    """Backup files in the backup directory, newest first."""
    return backup.list_backups()
