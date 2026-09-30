"""Statistics for the Stats page, cached briefly."""

import asyncio
import time

from app.config import get_settings
from app.repositories import logs as logs_repo
from app.repositories.database import Database

# get_stats() runs five aggregations over the whole table; the Stats page and
# its auto-refresh called it on every visit. Results are cached briefly and
# dropped whenever imports, cleanup or clear change the data.
_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}


def invalidate() -> None:
    _cache.clear()


async def get_stats(db: Database, tenant: str | None = None) -> dict:
    key = tenant or "all"
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < get_settings().stats_cache_seconds:
        return cached[1]
    # Single flight: concurrent requests for the same key share one computation.
    async with _locks.setdefault(key, asyncio.Lock()):
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < get_settings().stats_cache_seconds:
            return cached[1]
        result = await logs_repo.compute_stats(db, tenant)
        _cache[key] = (time.monotonic(), result)
        return result
