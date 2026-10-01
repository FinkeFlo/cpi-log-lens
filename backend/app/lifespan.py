"""Start-up and clean shutdown of the app process."""

import contextlib
import logging
import threading
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app import storage, tasks, watchdog
from app.config import get_settings
from app.repositories import fetch_runs as fetch_runs_repo
from app.repositories.database import Database
from app.services import fetch as fetch_service
from app.services import scheduler
from app.services import tenants as tenant_service

log = logging.getLogger("cpi")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start: open (and migrate) the database, seed tenants, start the background
    loops. Stop: cancel them and any running fetch, then checkpoint and close the
    database so no work is left in the WAL."""
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    if settings.db_storage_upgrade and settings.db_path.exists():
        _upgrade_storage()
    db = Database.open(settings.db_path)
    app.state.db = db
    fetch = fetch_service.FetchService(db)
    app.state.fetch = fetch
    watchdog_stop = threading.Event()
    try:
        interrupted = await fetch_runs_repo.mark_interrupted(db)
        if interrupted:
            log.warning("%d fetch job(s) were cut off by the last stop of the app; marked as interrupted", interrupted)
        await tenant_service.load_tenants_from_json(db)
        tasks.spawn(watchdog.heartbeat())
        if settings.watchdog_stall_seconds > 0:
            threading.Thread(
                target=watchdog.watchdog,
                args=(watchdog_stop, settings.watchdog_stall_seconds),
                name="loop-watchdog",
                daemon=True,
            ).start()
        if settings.retention_days > 0:
            tasks.spawn(scheduler.retention_loop(db, fetch))
        tasks.spawn(scheduler.schedule_loop(db, fetch))
        yield
    finally:
        log.info("shutting down")
        watchdog_stop.set()
        await fetch.shutdown()
        await tasks.cancel_all()
        await db.close()


def _upgrade_storage() -> None:
    """Opt-in (DB_STORAGE_UPGRADE): convert the database file to the compressed storage
    format before it is opened. On any problem the file stays as it is."""
    settings = get_settings()
    try:
        if not storage.needs_upgrade(settings.db_path):
            log.info("storage upgrade: the database already uses the compressed format")
            return
        storage.upgrade_file(
            settings.db_path,
            memory_limit=settings.duckdb_memory_limit,
            threads=settings.duckdb_threads,
            temp_dir=settings.duckdb_temp_dir,
        )
    except storage.StorageUpgradeError as e:
        log.error("storage upgrade skipped, the database is unchanged: %s", e)
