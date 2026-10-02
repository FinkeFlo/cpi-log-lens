"""Recurring fetch schedules and automatic retention, run by APScheduler.

Schedules stay in the fetch_schedules table; each enabled one becomes an interval
job of an in-memory AsyncIOScheduler, rebuilt at start and after every change.
A due schedule starts its fetch through the FetchService, so it shares the lock
with manual starts: when a fetch is already running it tries again shortly after,
instead of being lost. last_run_at is set only when the run succeeded."""

import json
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import tasks
from app.config import get_settings
from app.repositories import logs as logs_repo
from app.repositories import schedules as schedules_repo
from app.repositories.database import Database
from app.services import fetch as fetch_service
from app.services import stats

log = logging.getLogger("cpi")

# Length of a schedule "minute" in seconds (tests make it shorter).
MINUTE = 60.0
# A due schedule that finds a fetch running tries again after this many minutes.
BUSY_RETRY_MINUTES = 1
# Retention that finds a fetch running tries again after this many minutes.
RETENTION_RETRY_MINUTES = 5

_RETENTION_JOB = "retention"
_SCHEDULE_PREFIX = "schedule:"


def next_run(last_run_at: str | None, interval_minutes: int, now: datetime) -> datetime:
    """When a schedule is due next: one interval after its last successful run,
    or right away if it never ran or is overdue."""
    if not last_run_at:
        return now
    last = datetime.fromisoformat(last_run_at)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return max(last + timedelta(seconds=interval_minutes * MINUTE), now)


class ScheduleService:
    def __init__(self, db: Database, fetch: fetch_service.FetchService) -> None:
        self.db = db
        self.fetch = fetch
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    async def start(self) -> None:
        self._scheduler.start()
        await self.reload()
        settings = get_settings()
        if settings.retention_days > 0:
            self._scheduler.add_job(
                self._retention,
                IntervalTrigger(seconds=settings.retention_check_hours * 3600),
                id=_RETENTION_JOB,
                next_run_time=datetime.now(UTC),
                coalesce=True,
                max_instances=1,
                misfire_grace_time=None,
            )

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def job_ids(self) -> list[str]:
        return sorted(job.id for job in self._scheduler.get_jobs())

    async def reload(self) -> None:
        """Rebuild the schedule jobs from the database (after any change)."""
        schedules = await schedules_repo.get_schedules(self.db)
        wanted = {f"{_SCHEDULE_PREFIX}{s['id']}": s for s in schedules if s["enabled"]}
        for job in self._scheduler.get_jobs():
            if job.id.startswith(_SCHEDULE_PREFIX) and job.id not in wanted:
                job.remove()
        now = datetime.now(UTC)
        for job_id, sched in wanted.items():
            interval = sched["interval_minutes"] * MINUTE
            existing = self._scheduler.get_job(job_id)
            if existing is not None and existing.kwargs.get("interval") == interval:
                continue  # unchanged interval: keep its timing
            first = next_run(sched["last_run_at"], sched["interval_minutes"], now)
            self._scheduler.add_job(
                self._run_schedule,
                IntervalTrigger(seconds=interval, start_date=first),
                next_run_time=first,
                id=job_id,
                kwargs={"schedule_id": sched["id"], "interval": interval},
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=max(1, int(interval)),
            )

    async def _run_schedule(self, schedule_id: str, interval: float) -> None:
        job_id = f"{_SCHEDULE_PREFIX}{schedule_id}"
        try:
            sched = await schedules_repo.get_schedule(self.db, schedule_id)
            if sched is None or not sched["enabled"]:
                await self.reload()
                return
            try:
                await self._start(sched)
            except fetch_service.JobAlreadyRunning:
                self._retry(job_id, BUSY_RETRY_MINUTES)
                return
        except Exception as e:
            log.exception(f"schedule {schedule_id}: could not start: {e}")

    async def _start(self, sched: dict) -> fetch_service.FetchJob:
        """Start the fetch of a schedule; JobAlreadyRunning if a fetch runs."""
        params = fetch_service.FetchParams(
            tenants=json.loads(sched["tenants"]),
            log_types=json.loads(sched["log_types"]),
            hours=sched["hours"],
        )
        job = await self.fetch.start(params, trigger=f"{_SCHEDULE_PREFIX}{sched['id']}")
        log.info(
            f"schedule: started '{sched['name']}' "
            f"(tenants={params.tenants}, log_types={params.log_types}, hours={params.hours})"
        )
        tasks.spawn(self._record_success(sched["id"], job))
        return job

    async def _record_success(self, schedule_id: str, job: fetch_service.FetchJob) -> None:
        await job.ended.wait()
        if job.status == "done":
            await schedules_repo.touch_schedule_last_run(self.db, schedule_id)
        else:
            log.info("schedule %s: run %s ended %s; last_run_at unchanged", schedule_id, job.id[:8], job.status)

    def _retry(self, job_id: str, minutes: float) -> None:
        when = datetime.now(UTC) + timedelta(seconds=minutes * MINUTE)
        job = self._scheduler.get_job(job_id)
        if job is not None and (job.next_run_time is None or job.next_run_time > when):
            job.modify(next_run_time=when)

    async def _retention(self) -> None:
        """Delete entries older than RETENTION_DAYS. DuckDB has one writer, so this
        waits for a running fetch (it would only slow the import down) and tries
        again a few minutes later instead of skipping a whole interval."""
        if self.fetch.is_running():
            self._scheduler.add_job(
                self._retention,
                DateTrigger(datetime.now(UTC) + timedelta(seconds=RETENTION_RETRY_MINUTES * MINUTE)),
                id=f"{_RETENTION_JOB}-retry",
                replace_existing=True,
            )
            return
        days = get_settings().retention_days
        try:
            result = await logs_repo.cleanup_old_logs(self.db, days)
            stats.invalidate()
            if result["deleted"]:
                log.info(
                    f"retention: deleted {result['deleted']} log entries "
                    f"older than {days} days ({result['remaining']} remaining)."
                )
        except Exception as e:
            log.exception(f"retention: cleanup failed: {e}")
