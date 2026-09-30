"""CPI Log Lens — FastAPI app: API routers, the web UI and the process lifecycle.

Run with `uvicorn app.main:app`."""

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import errors
from app.api import admin, fetch, health, logs, middleware, query, schedules, stats, tenants
from app.config import Settings, get_settings
from app.lifespan import lifespan
from app.logging_config import setup_logging

log = logging.getLogger("cpi")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_format)
    app = FastAPI(title="CPI Log Lens", version=settings.app_version, lifespan=lifespan)
    errors.install(app)
    middleware.install(app, settings)
    for module in (health, tenants, fetch, schedules, logs, query, stats, admin):
        app.include_router(module.router)
    if settings.frontend_dir.exists():
        app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")
    else:
        log.warning("frontend directory %s not found; serving the API only", settings.frontend_dir)
    return app


app = create_app()
