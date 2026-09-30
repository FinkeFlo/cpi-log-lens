"""Statistics (Stats page)."""

from fastapi import APIRouter

from app.api.deps import DbDep
from app.services import stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def get_stats(db: DbDep, tenant: str | None = None):
    return await stats.get_stats(db, tenant)
