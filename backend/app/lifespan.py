"""Start-up and clean shutdown of the app process."""

import contextlib
import logging
import threading
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app import tasks, watchdog
from app.config import get_settings
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
    db = Database.open(settings.db_path)
    app.state.db = db
    watchdog_stop = threading.Event()
    try:
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
            tasks.spawn(scheduler.retention_loop(db))
        tasks.spawn(scheduler.schedule_loop(db))
        yield
    finally:
        log.info("shutting down")
        watchdog_stop.set()
        fetch_service.mark_cancelled_for_shutdown()
        await tasks.cancel_all()
        await db.close()
