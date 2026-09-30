"""
CPI Log Lens — FastAPI backend
Serves the frontend and provides REST + SSE API.
"""

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.logging_config import setup_logging

settings = get_settings()
setup_logging(settings.log_level, settings.log_format)

# Logging must be configured before these modules create their loggers.
from app import tasks, watchdog  # noqa: E402
from app.cpi import client as cpi_api  # noqa: E402
from app.repositories import app_settings as settings_repo  # noqa: E402
from app.repositories import database  # noqa: E402
from app.repositories import logs as logs_repo  # noqa: E402
from app.repositories import schedules as schedules_repo  # noqa: E402
from app.repositories import tenants as tenants_repo  # noqa: E402
from app.services import fetch as fetch_service  # noqa: E402
from app.services import scheduler, stats  # noqa: E402
from app.services import tenants as tenant_service  # noqa: E402
from app.tasks import spawn  # noqa: E402

log = logging.getLogger("cpi")


# ── App ───────────────────────────────────────────────────────────────────────
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start: open (and migrate) the database, seed tenants, start the background
    loops. Stop: cancel them and any running fetch, then checkpoint and close the
    database so no work is left in the WAL."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    await database.init_db(settings.db_path)
    db = await database.get_db()
    await tenant_service.load_tenants_from_json(db)
    watchdog_stop = threading.Event()
    spawn(watchdog.heartbeat())
    if settings.watchdog_stall_seconds > 0:
        threading.Thread(
            target=watchdog.watchdog,
            args=(watchdog_stop, settings.watchdog_stall_seconds),
            name="loop-watchdog",
            daemon=True,
        ).start()
    if settings.retention_days > 0:
        spawn(scheduler.retention_loop(db))
    spawn(scheduler.schedule_loop(db))
    try:
        yield
    finally:
        log.info("shutting down")
        watchdog_stop.set()
        fetch_service.mark_cancelled_for_shutdown()
        await tasks.cancel_all()
        await database.close_db()


app = FastAPI(title="CPI Log Lens", version=settings.app_version, lifespan=lifespan)


@app.exception_handler(database.DBBusyError)
async def db_busy(request, exc: database.DBBusyError):
    return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "5"})


# Safe defaults for an app that runs on the user's machine without login:
# - Only requests addressed to an allowed host name are served. This blocks
#   DNS rebinding, where a web page re-points its own domain at 127.0.0.1.
# - No CORS by default: the UI is served from the same origin, so other web
#   pages cannot read API responses. CORS_ORIGINS opts specific origins in.
# - Writes coming from another origin are rejected (see below), because a
#   page can still *send* simple cross-site requests without CORS.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def reject_cross_origin_writes(request, call_next):
    """Browsers attach an Origin header to cross-site POST/PUT/DELETE requests,
    including plain form posts that need no CORS preflight. Such a request
    from any page other than this app's own origin is refused, so a web page
    open in the same browser cannot create tenants, start fetches or clear
    the database. Requests without Origin (curl, scripts, LLM tools) are not
    affected."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin not in settings.cors_origins and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def request_log_and_no_cache_js(request, call_next):
    """Log every request with its duration (replaces uvicorn's access log) and
    prevent browser caching of app.js."""
    t0 = time.perf_counter()
    response = await call_next(request)
    dur_ms = (time.perf_counter() - t0) * 1000
    level = logging.DEBUG if request.url.path in ("/healthz", "/readyz") else logging.INFO
    if dur_ms > 1000 or response.status_code >= 500:
        level = logging.WARNING
    log.log(
        level,
        "%s %s %s %.0fms",
        request.method,
        request.url.path,
        response.status_code,
        dur_ms,
        extra={
            "fields": {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "dur_ms": round(dur_ms, 1),
            }
        },
    )
    if request.url.path.endswith(".js"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ── Pydantic models ───────────────────────────────────────────────────────────
# Tenant ids end up in URLs and directory names: lowercase letters, digits,
# "-" and "_" only. "all" is reserved as the "every tenant" sentinel.
TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
LogType = Literal["trace", "http"]
MAX_HOURS = 24 * 365


class TenantCreate(BaseModel):
    id: str = Field(pattern=TENANT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    api_url: str = Field(pattern=r"^https?://\S+$", max_length=500)
    oauth_url: str = Field(pattern=r"^https?://\S+$", max_length=500)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(max_length=2000)  # empty on update = keep the stored one

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v == "all":
            raise ValueError('"all" is reserved')
        return v


class FetchRequest(BaseModel):
    tenants: list[str] = Field(default=["all"], min_length=1)  # ["all"] or tenant ids
    log_types: list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours: int = Field(default=24, ge=0, le=MAX_HOURS)  # 0 = no filter (all available)


class ScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    tenants: list[str] = Field(default=["all"], min_length=1)
    log_types: list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours: int = Field(default=1, ge=0, le=MAX_HOURS)  # time range pulled on every run
    interval_minutes: int = Field(default=15, ge=5, le=7 * 24 * 60)  # how often to run
    enabled: bool = True


class DefaultFetchConfig(BaseModel):
    tenants: list[str] = Field(default=["all"], min_length=1)
    log_types: list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours: int = Field(default=24, ge=0, le=MAX_HOURS)


def _check_datetime(value: str | None, field: str) -> None:
    """date_from/date_to must be an ISO date or datetime; anything else used to
    reach DuckDB and fail there with a 500."""
    if value:
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise HTTPException(422, f"{field} must be YYYY-MM-DD or YYYY-MM-DD HH:MM:SS") from None


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz():
    """Liveness: answers as long as the event loop runs; no database access."""
    return {"ok": True, "version": settings.app_version, "loop_lag_s": round(watchdog.loop_lag(), 2)}


@app.get("/readyz")
async def readyz():
    """Readiness: the database answers a trivial query within 2 s."""
    conn = await database.get_db()
    try:
        await asyncio.wait_for(conn.read(conn.fetch_val, "SELECT 1"), 2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)
    job = fetch_service.active_job
    return {"ok": True, "fetch_job": job.status if job else "idle"}


# ── Tenant endpoints ──────────────────────────────────────────────────────────
@app.get("/api/tenants")
async def list_tenants():
    conn = await database.get_db()
    tenants = await tenants_repo.get_tenants(conn)
    # Mask secrets in response
    for t in tenants:
        t["client_secret"] = "••••••••" if t.get("client_secret") else ""
    return tenants


@app.post("/api/tenants", status_code=201)
async def create_tenant(body: TenantCreate):
    if not body.client_secret:
        raise HTTPException(422, "client_secret is required")
    conn = await database.get_db()
    await tenants_repo.upsert_tenant(
        conn,
        body.id,
        body.name,
        body.api_url,
        body.oauth_url,
        body.client_id,
        body.client_secret,
    )
    return {"ok": True}


@app.put("/api/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, body: TenantCreate):
    conn = await database.get_db()
    existing = await tenants_repo.get_tenant(conn, tenant_id)
    if not existing:
        raise HTTPException(404, "Tenant not found")
    # Keep existing secret if an empty or masked value is submitted —
    # the edit form never pre-fills the stored secret.
    secret = body.client_secret
    if not secret or set(secret) == {"•"}:
        secret = existing["client_secret"]
    await tenants_repo.upsert_tenant(
        conn,
        tenant_id,
        body.name,
        body.api_url,
        body.oauth_url,
        body.client_id,
        secret,
    )
    return {"ok": True}


@app.delete("/api/tenants/{tenant_id}")
async def remove_tenant(tenant_id: str):
    conn = await database.get_db()
    await tenants_repo.delete_tenant(conn, tenant_id)
    return {"ok": True}


@app.post("/api/tenants/{tenant_id}/test")
async def test_tenant(tenant_id: str):
    conn = await database.get_db()
    try:
        tenant = await tenants_repo.get_tenant(conn, tenant_id)
        if not tenant:
            raise HTTPException(404, "Tenant not found")
        if tenant_service.is_demo(tenant):
            return {"ok": True, "demo": True}
        token = await cpi_api.get_token(tenant["oauth_url"], tenant["client_id"], tenant["client_secret"])
        return {"ok": bool(token), "token_preview": token[:12] + "…"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/demo")
async def start_demo():
    """Create the demo tenant (if needed) and import the bundled sample logs."""
    await tenant_service.ensure_demo_tenant(await database.get_db())
    return await fetch_logs(FetchRequest(tenants=[tenant_service.DEMO_TENANT_ID], log_types=["trace"], hours=0))


# ── Fetch: start background job ───────────────────────────────────────────────
@app.post("/api/fetch")
async def fetch_logs(body: FetchRequest):
    """Start a background fetch job. Returns job id immediately."""
    job = fetch_service.start(await database.get_db(), fetch_service.FetchParams(**body.model_dump()))
    if job is None:
        running = fetch_service.active_job
        assert running is not None
        return {"ok": False, "error": "A fetch is already running.", "job_id": running.id}
    return {"ok": True, "job_id": job.id}


@app.get("/api/fetch/status")
async def fetch_status():
    """Return current snapshot of the active job (for polling or initial state)."""
    job = fetch_service.active_job
    if not job:
        return {"status": "idle"}
    return job.snapshot()


@app.post("/api/fetch/cancel")
async def fetch_cancel():
    """Request cancellation of the active job. run_fetch() checks
    `cancel_requested` at each file boundary and stops cleanly (finishes the
    file currently in flight rather than being killed mid-write, so the DB
    stays consistent) instead of requiring a full container restart."""
    job = fetch_service.request_cancel()
    if job is None:
        return {"ok": False, "error": "No fetch is running."}
    return {"ok": True, "job_id": job.id}


# ── Fetch: default form config ────────────────────────────────────────────────
@app.get("/api/fetch/default-config")
async def get_default_fetch_config():
    """Return the saved default fetch form config (tenants/log_types/hours),
    or null if none has been saved yet — the frontend falls back to
    'all tenants + all log types' in that case."""
    conn = await database.get_db()
    raw = await settings_repo.get_setting(conn, "default_fetch_config")
    return json.loads(raw) if raw else None


@app.put("/api/fetch/default-config")
async def set_default_fetch_config(body: DefaultFetchConfig):
    """Persist the current fetch form selection as the default shown on
    next page load, instead of always defaulting to all tenants/log types."""
    conn = await database.get_db()
    await settings_repo.set_setting(conn, "default_fetch_config", body.model_dump_json())
    return {"ok": True}


# ── Fetch schedules (recurring pulls) ─────────────────────────────────────────
@app.get("/api/schedules")
async def list_schedules():
    conn = await database.get_db()
    schedules = await schedules_repo.get_schedules(conn)
    for s in schedules:
        s["tenants"] = json.loads(s["tenants"])
        s["log_types"] = json.loads(s["log_types"])
    return schedules


@app.post("/api/schedules", status_code=201)
async def create_schedule(body: ScheduleRequest):
    conn = await database.get_db()
    schedule_id = str(uuid.uuid4())
    await schedules_repo.create_schedule(
        conn,
        schedule_id,
        body.name,
        json.dumps(body.tenants),
        json.dumps(body.log_types),
        body.hours,
        body.interval_minutes,
        body.enabled,
    )
    return {"ok": True, "id": schedule_id}


@app.put("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleRequest):
    conn = await database.get_db()
    existing = await schedules_repo.get_schedule(conn, schedule_id)
    if not existing:
        raise HTTPException(404, "Schedule not found")
    await schedules_repo.update_schedule(
        conn,
        schedule_id,
        body.name,
        json.dumps(body.tenants),
        json.dumps(body.log_types),
        body.hours,
        body.interval_minutes,
        body.enabled,
    )
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
async def remove_schedule(schedule_id: str):
    conn = await database.get_db()
    await schedules_repo.delete_schedule(conn, schedule_id)
    return {"ok": True}


@app.get("/api/fetch/stream")
async def fetch_stream():
    """SSE stream — attach to the active job from any page/tab."""
    return StreamingResponse(
        fetch_service.event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── LLM query API ─────────────────────────────────────────────────────────────


class LLMQueryRequest(BaseModel):
    tenant: str | None = None  # tenant id, or null for all
    level: str | None = None  # ERROR | WARN | INFO | DEBUG
    iflow: str | None = None  # partial iflow name match
    grep: str | None = None  # full-text search in message/logger
    date_from: str | None = None  # ISO datetime, e.g. "2024-01-01 00:00:00"
    date_to: str | None = None  # ISO datetime, e.g. "2024-01-31 23:59:59"
    limit: int = 50  # max log entries returned (1–200)


@app.get("/api/query/schema")
async def llm_query_schema():
    """Describes the query API for LLM tool-use / function-calling."""
    conn = await database.get_db()
    tenants = await tenants_repo.get_tenants(conn)

    return {
        "description": (
            "CPI Log Lens query API. Use POST /api/query to search SAP CPI log entries. "
            "Use GET /api/stats to get aggregated statistics."
        ),
        "endpoints": {
            "POST /api/query": {
                "description": "Search log entries with optional filters.",
                "body": {
                    "tenant": "string | null — tenant id to filter; null means all tenants",
                    "level": "string | null — log level: ERROR, WARN, INFO, DEBUG",
                    "iflow": "string | null — partial iflow name (case-insensitive LIKE match)",
                    "grep": "string | null — full-text search in message and logger fields",
                    "date_from": "string | null — start datetime 'YYYY-MM-DD HH:MM:SS'",
                    "date_to": "string | null — end datetime 'YYYY-MM-DD HH:MM:SS'",
                    "limit": "integer 1–200 — max entries to return (default 50)",
                },
                "response": {
                    "total_matching": "total rows matching the filter (may exceed limit)",
                    "returned": "number of rows returned",
                    "summary": "short natural-language summary of the result",
                    "items": "array of log entries",
                    "item_fields": "id, tenant, log_type, filename, timestamp, level, logger, iflow, message, ip, node",
                },
            },
            "GET /api/stats": {
                "description": "Aggregated statistics (level distribution, top error iflows, timeline).",
                "params": {"tenant": "optional tenant id"},
            },
            "GET /api/tenants": {
                "description": "List configured tenants.",
            },
        },
        "available_tenants": [{"id": t["id"], "name": t["name"]} for t in tenants],
        "tip": "Start with GET /api/query/schema to understand the data, then POST /api/query with filters.",
    }


@app.post("/api/query")
async def llm_query(req: LLMQueryRequest):
    """LLM-friendly log query endpoint. Returns matching entries plus a natural-language summary."""
    _check_datetime(req.date_from, "date_from")
    _check_datetime(req.date_to, "date_to")
    limit = max(1, min(req.limit, 200))
    conn = await database.get_db()
    result = await logs_repo.query_logs(
        conn,
        tenant=req.tenant,
        level=req.level,
        iflow=req.iflow,
        grep=req.grep,
        date_from=req.date_from,
        date_to=req.date_to,
        page=1,
        page_size=limit,
    )

    total = result["total"]
    items = result["items"]

    # Build a short summary for LLMs to orient themselves
    parts = []
    if req.tenant:
        parts.append(f"tenant={req.tenant}")
    if req.level:
        parts.append(f"level={req.level.upper()}")
    if req.iflow:
        parts.append(f"iflow~'{req.iflow}'")
    if req.grep:
        parts.append(f"grep='{req.grep}'")
    if req.date_from or req.date_to:
        parts.append(f"range=[{req.date_from or '...'} → {req.date_to or '...'}]")

    filter_desc = ", ".join(parts) if parts else "no filters"
    summary = (
        f"Found {total} log entr{'y' if total == 1 else 'ies'} matching {filter_desc}. "
        f"Returning {len(items)} of {total}."
    )
    if total > limit:
        summary += " Use a stricter filter or increase limit (max 200) to see more."

    return {
        "total_matching": total,
        "returned": len(items),
        "summary": summary,
        "items": items,
    }


# ── Log query ─────────────────────────────────────────────────────────────────
@app.get("/api/logs")
async def get_logs(
    tenant: str | None = None,
    level: str | None = None,
    iflow: str | None = None,
    grep: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(100, ge=1, le=500),
):
    _check_datetime(date_from, "date_from")
    _check_datetime(date_to, "date_to")
    conn = await database.get_db()
    return await logs_repo.query_logs(
        conn,
        tenant=tenant,
        level=level,
        iflow=iflow,
        grep=grep,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


@app.get("/api/logs/{entry_id}")
async def get_log_entry(entry_id: int):
    conn = await database.get_db()
    entry = await logs_repo.get_log_entry(conn, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    return entry


# ── Stats ─────────────────────────────────────────────────────────────────────
@app.get("/api/stats")
async def get_stats(tenant: str | None = None):
    conn = await database.get_db()
    return await stats.get_stats(conn, tenant)


# ── DB info ───────────────────────────────────────────────────────────────────
@app.get("/api/db/info")
async def db_info():
    conn = await database.get_db()
    return await logs_repo.get_db_info(conn, settings.db_path)


@app.post("/api/db/clear")
async def db_clear():
    conn = await database.get_db()
    await logs_repo.clear_db(conn)
    stats.invalidate()
    return {"ok": True}


class CleanupRequest(BaseModel):
    older_than_days: int = Field(ge=1, le=36500)  # delete entries older than this many days
    tenant: str | None = None  # optional: restrict to one tenant


@app.post("/api/db/cleanup")
async def db_cleanup(req: CleanupRequest):
    """Delete log entries older than N days, optionally filtered by tenant."""
    conn = await database.get_db()
    result = await logs_repo.cleanup_old_logs(conn, req.older_than_days, req.tenant)
    stats.invalidate()
    return {"ok": True, **result}


# ── Serve frontend ────────────────────────────────────────────────────────────
if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")
else:
    log.warning("frontend directory %s not found; serving the API only", settings.frontend_dir)
