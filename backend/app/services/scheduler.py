"""Background loops: recurring fetch schedules and automatic retention."""

import asyncio
import json
import logging
import time
from datetime import datetime

from app.config import get_settings
from app.repositories import logs as logs_repo
from app.repositories import schedules as schedules_repo
from app.repositories.database import Database
from app.services import fetch as fetch_service
from app.services import stats

log = logging.getLogger("cpi")


async def retention_loop(db: Database, fetch: fetch_service.FetchService) -> None:
    """Periodically delete logs older than RETENTION_DAYS.
    Runs once immediately at startup, then every RETENTION_CHECK_HOURS.
    Skips a tick (retrying next interval) while a fetch job is active —
    DuckDB allows only one writer at a time, so running this concurrently
    with an in-progress import would just serialize behind/ahead of it and
    slow down the download instead of running for free in the background."""
    settings = get_settings()
    while True:
        if fetch.is_running():
            await asyncio.sleep(settings.retention_check_hours * 3600)
            continue
        try:
            result = await logs_repo.cleanup_old_logs(db, settings.retention_days)
            stats.invalidate()
            if result["deleted"]:
                log.info(
                    f"retention: deleted {result['deleted']} log entries "
                    f"older than {settings.retention_days} days ({result['remaining']} remaining)."
                )
        except Exception as e:
            log.exception(f"retention: cleanup failed: {e}")
        await asyncio.sleep(settings.retention_check_hours * 3600)


async def schedule_loop(db: Database, fetch: fetch_service.FetchService) -> None:
    """Every SCHEDULE_CHECK_SECONDS, check enabled fetch_schedules and kick off
    a fetch job for any that are due (now >= last_run_at + interval_minutes,
    or never run before). Only one fetch job can run at a time (shared with
    manual /api/fetch); a due schedule that finds a job already running is
    simply retried on the next tick instead of being queued."""
    settings = get_settings()
    while True:
        try:
            if not fetch.is_running():
                schedules = await schedules_repo.get_schedules(db)

                now = time.time()
                for sched in schedules:
                    if not sched["enabled"]:
                        continue
                    if sched["last_run_at"]:
                        last_ts = datetime.fromisoformat(sched["last_run_at"]).timestamp()
                        if now - last_ts < sched["interval_minutes"] * 60:
                            continue

                    params = fetch_service.FetchParams(
                        tenants=json.loads(sched["tenants"]),
                        log_types=json.loads(sched["log_types"]),
                        hours=sched["hours"],
                    )
                    try:
                        await fetch.start(params, trigger=f"schedule:{sched['id']}")
                    except fetch_service.JobAlreadyRunning:
                        break  # started manually in the meantime; retried on the next tick
                    await schedules_repo.touch_schedule_last_run(db, sched["id"])
                    log.info(
                        f"schedule: started '{sched['name']}' "
                        f"(tenants={params.tenants}, log_types={params.log_types}, hours={params.hours})"
                    )
                    break  # one job at a time — remaining due schedules wait for next tick
        except Exception as e:
            log.exception(f"schedule: loop error: {e}")
        await asyncio.sleep(settings.schedule_check_seconds)
