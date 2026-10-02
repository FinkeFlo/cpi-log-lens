"""Log list and log entry detail (Browse page)."""

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbDep
from app.api.schemas import check_date_range, check_datetime, to_datetime
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
    page: int | None = Query(None, ge=1, le=1_000_000),
    cursor: str | None = Query(None, max_length=40),
    at: str | None = None,
    page_size: int = Query(100, ge=1, le=500),
    count: bool = True,
):
    """Log entries, newest first. Page with `older_cursor`/`newer_cursor` from the answer
    (passed as `cursor`), start at a time with `at`, or use page numbers (`page`)."""
    date_from = check_datetime(date_from, "date_from")
    date_to = check_datetime(date_to, "date_to")
    check_date_range(date_from, date_to)
    if sum(value is not None for value in (page, cursor, at)) > 1:
        raise HTTPException(422, "Use only one of page, cursor and at")
    position = None
    if cursor is not None:
        try:
            position = logs_repo.Cursor.parse(cursor)
        except ValueError:
            raise HTTPException(422, "cursor is invalid: pass newer_cursor or older_cursor of an answer") from None
    if at is not None:
        position = logs_repo.Cursor.at(to_datetime(check_datetime(at, "at"), end_of_day=True))
    return await logs_repo.query_logs(
        db,
        tenant=tenant,
        level=level,
        iflow=iflow,
        grep=grep,
        date_from=date_from,
        date_to=date_to,
        page=page,
        cursor=position,
        page_size=page_size,
        count=count,
    )


@router.get("/{entry_id}")
async def get_log_entry(entry_id: int, db: DbDep):
    entry = await logs_repo.get_log_entry(db, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    return entry
