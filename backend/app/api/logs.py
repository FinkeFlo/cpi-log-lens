"""Log list and log entry detail (Browse page)."""

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbDep
from app.api.schemas import check_date_range, check_datetime
from app.repositories import logs as logs_repo

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/iflows")
async def get_iflows(db: DbDep, tenant: str | None = None):
    return {"items": await logs_repo.list_iflows(db, tenant)}


@router.get("")
async def get_logs(
    db: DbDep,
    tenant: str | None = None,
    level: str | None = None,
    iflow: str | None = None,
    grep: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(100, ge=1, le=500),
):
    date_from = check_datetime(date_from, "date_from")
    date_to = check_datetime(date_to, "date_to")
    check_date_range(date_from, date_to)
    return await logs_repo.query_logs(
        db,
        tenant=tenant,
        level=level,
        iflow=iflow,
        grep=grep,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


@router.get("/{entry_id}")
async def get_log_entry(entry_id: int, db: DbDep):
    entry = await logs_repo.get_log_entry(db, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    return entry
