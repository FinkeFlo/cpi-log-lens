"""
CPI Log Lens — FastAPI backend
Serves the frontend and provides REST + SSE API.
"""
import asyncio
import faulthandler
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Literal, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from logging_config import setup_logging

setup_logging()

import db as database  # noqa: E402  (logging must be configured first)
import api as cpi_api  # noqa: E402

log = logging.getLogger("cpi")

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH   = Path(os.getenv("DB_PATH",   "cpi_logs.duckdb"))
LOGS_DIR  = Path(os.getenv("LOGS_DIR",  "logs"))
MOCK      = os.getenv("MOCK", "false").lower() == "true"
# Concurrent file downloads per tenant/log_type during a fetch job. CPI's
# LogFiles $value endpoint has high per-request latency (server-side
# decompression, ~30-90s/file observed) so parallelism is the main client-side
# lever we have; tune via env if your tenant tolerates more/less concurrency.
FETCH_CONCURRENCY = int(os.getenv("FETCH_CONCURRENCY", "4"))
# Optional automatic retention: if set (>0), a background task periodically
# deletes log entries older than this many days across all tenants, so DB
# size doesn't grow unbounded without someone remembering to call
# /api/db/cleanup manually. Unset/0 (default) disables it — fully opt-in.
RETENTION_DAYS         = int(os.getenv("RETENTION_DAYS", "0"))
RETENTION_CHECK_HOURS  = float(os.getenv("RETENTION_CHECK_HOURS", "24"))
MOCK_DIR  = Path(__file__).parent / "mock"
FRONTEND  = Path(os.getenv("FRONTEND_DIR", str(Path(__file__).parent.parent / "frontend")))

database.DB_PATH = DB_PATH

# ── Global fetch-job state ────────────────────────────────────────────────────
@dataclass
class FetchJob:
    id:            str
    status:        str  = "running"  # running | done | error | cancelled
    status_msg:    str  = ""
    done:          int  = 0
    total:         int  = 0
    current_file:  str  = ""
    current_tenant:   str = ""
    current_log_type: str = ""
    imported:      int  = 0
    error_msg:     str  = ""
    cancel_requested: bool = False
    # Listeners waiting for new events (one queue per SSE subscriber)
    _listeners:    list = field(default_factory=list)

    def push(self, event: dict):
        """Broadcast an event to all active SSE listeners."""
        if event.get("type") in ("warn", "error", "done", "cancelled"):
            level = logging.INFO if event["type"] in ("done", "cancelled") else logging.WARNING
            log.log(level, "fetch job %s: %s %s", self.id[:8], event["type"],
                    event.get("msg", f"imported={event.get('imported')}"),
                    extra={"fields": {"job_id": self.id, "event": event["type"]}})
        for q in list(self._listeners):
            q.put_nowait(event)

    def attach(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.append(q)
        return q

    def detach(self, q: asyncio.Queue):
        try:
            self._listeners.remove(q)
        except ValueError:
            pass

# Single active job (only one fetch at a time)
_active_job: Optional[FetchJob] = None

# ── App ───────────────────────────────────────────────────────────────────────
# Set at image build time (Dockerfile ARG VERSION); "dev" when run from source.
APP_VERSION = os.getenv("APP_VERSION", "dev")

app = FastAPI(title="CPI Log Lens", version=APP_VERSION)

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
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
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
        if origin and origin not in CORS_ORIGINS and urlsplit(origin).netloc != request.headers.get("host"):
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
    log.log(level, "%s %s %s %.0fms", request.method, request.url.path, response.status_code, dur_ms,
            extra={"fields": {"method": request.method, "path": request.url.path,
                              "status": response.status_code, "dur_ms": round(dur_ms, 1)}})
    if request.url.path.endswith(".js"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


SCHEDULE_CHECK_SECONDS = int(os.getenv("SCHEDULE_CHECK_SECONDS", "60"))
# If the event loop does not advance for this long, the process dumps all
# thread stacks and exits so the container restarts (0 disables the watchdog).
WATCHDOG_STALL_SECONDS = float(os.getenv("WATCHDOG_STALL_SECONDS", "120"))
_loop_heartbeat = time.monotonic()


@app.on_event("startup")
async def startup():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    await database.init_db(DB_PATH)
    await _load_tenants_from_json()
    asyncio.create_task(_heartbeat())
    if WATCHDOG_STALL_SECONDS > 0:
        threading.Thread(target=_watchdog, name="loop-watchdog", daemon=True).start()
    if RETENTION_DAYS > 0:
        asyncio.create_task(_retention_loop())
    asyncio.create_task(_schedule_loop())


async def _heartbeat():
    global _loop_heartbeat
    while True:
        _loop_heartbeat = time.monotonic()
        await asyncio.sleep(1)


def _watchdog():
    """Runs in its own thread. A blocked event loop means every request hangs
    while the process looks alive, so no restart policy would kick in. After
    WATCHDOG_STALL_SECONDS without a heartbeat, dump all stacks (for the
    post-mortem) and exit hard; Docker's restart policy brings the app back."""
    while True:
        time.sleep(5)
        stalled = time.monotonic() - _loop_heartbeat
        if stalled > WATCHDOG_STALL_SECONDS:
            log.critical("event loop stalled for %.0fs, dumping stacks and exiting", stalled)
            faulthandler.dump_traceback(all_threads=True)
            os._exit(70)


async def _retention_loop():
    """Background task: periodically delete logs older than RETENTION_DAYS.
    Runs once immediately at startup, then every RETENTION_CHECK_HOURS.
    Skips a tick (retrying next interval) while a fetch job is active —
    DuckDB allows only one writer at a time, so running this concurrently
    with an in-progress import would just serialize behind/ahead of it and
    slow down the download instead of running for free in the background."""
    while True:
        if _active_job and _active_job.status == "running":
            await asyncio.sleep(RETENTION_CHECK_HOURS * 3600)
            continue
        try:
            conn = await database.get_db(DB_PATH)
            result = await database.cleanup_old_logs(conn, RETENTION_DAYS)
            if result["deleted"]:
                log.info(f"retention: deleted {result['deleted']} log entries "
                      f"older than {RETENTION_DAYS} days ({result['remaining']} remaining).")
        except Exception as e:
            log.exception(f"retention: cleanup failed: {e}")
        await asyncio.sleep(RETENTION_CHECK_HOURS * 3600)


async def _schedule_loop():
    """Background task: every SCHEDULE_CHECK_SECONDS, check enabled
    fetch_schedules and kick off a fetch job for any that are due
    (now >= last_run_at + interval_minutes, or never run before).
    Only one fetch job can run at a time (shared with manual /api/fetch);
    a due schedule that finds a job already running is simply retried on
    the next tick instead of being queued."""
    global _active_job
    while True:
        try:
            if not (_active_job and _active_job.status == "running"):
                conn = await database.get_db(DB_PATH)
                try:
                    schedules = await database.get_schedules(conn)
                finally:
                    await conn.close()

                now = time.time()
                for sched in schedules:
                    if not sched["enabled"]:
                        continue
                    if sched["last_run_at"]:
                        last_ts = datetime.fromisoformat(sched["last_run_at"]).timestamp()
                        if now - last_ts < sched["interval_minutes"] * 60:
                            continue

                    conn2 = await database.get_db(DB_PATH)
                    try:
                        await database.touch_schedule_last_run(conn2, sched["id"])
                    finally:
                        await conn2.close()

                    body = FetchRequest(
                        tenants=json.loads(sched["tenants"]),
                        log_types=json.loads(sched["log_types"]),
                        hours=sched["hours"],
                    )
                    job = FetchJob(id=str(uuid.uuid4()))
                    _active_job = job
                    log.info(f"schedule: starting '{sched['name']}' "
                          f"(tenants={body.tenants}, log_types={body.log_types}, hours={body.hours})")
                    asyncio.create_task(_run_fetch(job, body))
                    break  # one job at a time — remaining due schedules wait for next tick
        except Exception as e:
            log.exception(f"schedule: loop error: {e}")
        await asyncio.sleep(SCHEDULE_CHECK_SECONDS)


# How TENANTS_CONFIG is applied on start:
#   create (default) — add tenants that don't exist yet; edits made in the UI stay
#   sync             — the file is the source of truth; its values overwrite the DB
TENANTS_SEED_MODE = os.getenv("TENANTS_SEED_MODE", "create").lower()


async def _load_tenants_from_json():
    """Seed tenants from the optional TENANTS_CONFIG file (JSON with comments)."""
    config_path = Path(os.getenv("TENANTS_CONFIG", "/config/tenants.jsonc"))
    if not config_path.is_file():
        if config_path.exists():
            log.warning("%s is not a file, ignoring it", config_path)
        return

    try:
        import json5
        data = json5.loads(config_path.read_text())
        tenant_list = data.get("tenants", [])
    except Exception as e:
        log.warning(f"could not parse {config_path}: {e}")
        return

    conn = await database.get_db(DB_PATH)
    try:
        added = updated = 0
        for t in tenant_list:
            tid    = t.get("id", "").strip().lower()
            name   = t.get("name", tid.upper())
            api    = t.get("api_url", "")
            oauth  = t.get("oauth_url", "")
            cid    = t.get("client_id", "")
            secret = t.get("client_secret", "")
            if not (tid and api and cid):
                continue
            exists = await database.get_tenant(conn, tid) is not None
            if exists and TENANTS_SEED_MODE != "sync":
                continue
            await database.upsert_tenant(conn, tid, name, api, oauth, cid, secret)
            updated += exists
            added += not exists
        log.info("tenants from %s: %d added, %d updated (mode %s)",
                 config_path, added, updated, TENANTS_SEED_MODE)
    finally:
        await conn.close()


# ── Pydantic models ───────────────────────────────────────────────────────────
# Tenant ids end up in URLs and directory names: lowercase letters, digits,
# "-" and "_" only. "all" is reserved as the "every tenant" sentinel.
TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
LogType = Literal["trace", "http"]
MAX_HOURS = 24 * 365


class TenantCreate(BaseModel):
    id:            str = Field(pattern=TENANT_ID_PATTERN)
    name:          str = Field(min_length=1, max_length=100)
    api_url:       str = Field(pattern=r"^https?://\S+$", max_length=500)
    oauth_url:     str = Field(pattern=r"^https?://\S+$", max_length=500)
    client_id:     str = Field(min_length=1, max_length=500)
    client_secret: str = Field(max_length=2000)  # empty on update = keep the stored one

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v == "all":
            raise ValueError('"all" is reserved')
        return v


class FetchRequest(BaseModel):
    tenants:   list[str] = Field(default=["all"], min_length=1)  # ["all"] or tenant ids
    log_types: list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours:     int = Field(default=24, ge=0, le=MAX_HOURS)  # 0 = no filter (all available)


class ScheduleRequest(BaseModel):
    name:             str = Field(min_length=1, max_length=100)
    tenants:          list[str] = Field(default=["all"], min_length=1)
    log_types:        list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours:            int = Field(default=1, ge=0, le=MAX_HOURS)   # time range pulled on every run
    interval_minutes: int = Field(default=15, ge=5, le=7 * 24 * 60)  # how often to run
    enabled:          bool = True


class DefaultFetchConfig(BaseModel):
    tenants:   list[str] = Field(default=["all"], min_length=1)
    log_types: list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours:     int = Field(default=24, ge=0, le=MAX_HOURS)


def _check_datetime(value: Optional[str], field: str) -> None:
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
    return {"ok": True, "version": APP_VERSION, "loop_lag_s": round(time.monotonic() - _loop_heartbeat, 2)}


@app.get("/readyz")
async def readyz():
    """Readiness: the database answers a trivial query within 2 s."""
    conn = await database.get_db(DB_PATH)
    try:
        await asyncio.wait_for(conn.read(conn._fetchone_val, "SELECT 1"), 2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)
    return {"ok": True, "fetch_job": _active_job.status if _active_job else "idle"}


# ── Tenant endpoints ──────────────────────────────────────────────────────────
@app.get("/api/tenants")
async def list_tenants():
    conn = await database.get_db(DB_PATH)
    try:
        tenants = await database.get_tenants(conn)
        # Mask secrets in response
        for t in tenants:
            t["client_secret"] = "••••••••" if t.get("client_secret") else ""
        return tenants
    finally:
        await conn.close()


@app.post("/api/tenants", status_code=201)
async def create_tenant(body: TenantCreate):
    if not body.client_secret:
        raise HTTPException(422, "client_secret is required")
    conn = await database.get_db(DB_PATH)
    try:
        await database.upsert_tenant(
            conn, body.id, body.name, body.api_url,
            body.oauth_url, body.client_id, body.client_secret,
        )
        return {"ok": True}
    finally:
        await conn.close()


@app.put("/api/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, body: TenantCreate):
    conn = await database.get_db(DB_PATH)
    try:
        existing = await database.get_tenant(conn, tenant_id)
        if not existing:
            raise HTTPException(404, "Tenant not found")
        # Keep existing secret if an empty or masked value is submitted —
        # the edit form never pre-fills the stored secret.
        secret = body.client_secret
        if not secret or set(secret) == {"•"}:
            secret = existing["client_secret"]
        await database.upsert_tenant(
            conn, tenant_id, body.name, body.api_url,
            body.oauth_url, body.client_id, secret,
        )
        return {"ok": True}
    finally:
        await conn.close()


@app.delete("/api/tenants/{tenant_id}")
async def remove_tenant(tenant_id: str):
    conn = await database.get_db(DB_PATH)
    try:
        await database.delete_tenant(conn, tenant_id)
        return {"ok": True}
    finally:
        await conn.close()


@app.post("/api/tenants/{tenant_id}/test")
async def test_tenant(tenant_id: str):
    conn = await database.get_db(DB_PATH)
    try:
        tenant = await database.get_tenant(conn, tenant_id)
        if not tenant:
            raise HTTPException(404, "Tenant not found")
        token = await cpi_api.get_token(
            tenant["oauth_url"], tenant["client_id"], tenant["client_secret"]
        )
        return {"ok": bool(token), "token_preview": token[:12] + "…"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()


# ── Fetch: start background job ───────────────────────────────────────────────
@app.post("/api/fetch")
async def fetch_logs(body: FetchRequest):
    """Start a background fetch job. Returns job id immediately."""
    global _active_job
    if _active_job and _active_job.status == "running":
        return {"ok": False, "error": "A fetch is already running.", "job_id": _active_job.id}

    job = FetchJob(id=str(uuid.uuid4()))
    _active_job = job

    asyncio.create_task(_run_fetch(job, body))
    return {"ok": True, "job_id": job.id}


@app.get("/api/fetch/status")
async def fetch_status():
    """Return current snapshot of the active job (for polling or initial state)."""
    if not _active_job:
        return {"status": "idle"}
    return {
        "job_id":       _active_job.id,
        "status":       _active_job.status,
        "status_msg":   _active_job.status_msg,
        "done":         _active_job.done,
        "total":        _active_job.total,
        "current_file": _active_job.current_file,
        "current_tenant":   _active_job.current_tenant,
        "current_log_type": _active_job.current_log_type,
        "imported":     _active_job.imported,
        "error_msg":    _active_job.error_msg,
    }


@app.post("/api/fetch/cancel")
async def fetch_cancel():
    """Request cancellation of the active job. _run_fetch() checks
    `cancel_requested` at each file boundary and stops cleanly (finishes the
    file currently in flight rather than being killed mid-write, so the DB
    stays consistent) instead of requiring a full container restart."""
    if not _active_job or _active_job.status != "running":
        return {"ok": False, "error": "No fetch is running."}
    _active_job.cancel_requested = True
    _active_job.push({"type": "status", "msg": "Cancelling…"})
    return {"ok": True, "job_id": _active_job.id}


# ── Fetch: default form config ────────────────────────────────────────────────
@app.get("/api/fetch/default-config")
async def get_default_fetch_config():
    """Return the saved default fetch form config (tenants/log_types/hours),
    or null if none has been saved yet — the frontend falls back to
    'all tenants + all log types' in that case."""
    conn = await database.get_db(DB_PATH)
    try:
        raw = await database.get_setting(conn, "default_fetch_config")
        return json.loads(raw) if raw else None
    finally:
        await conn.close()


@app.put("/api/fetch/default-config")
async def set_default_fetch_config(body: DefaultFetchConfig):
    """Persist the current fetch form selection as the default shown on
    next page load, instead of always defaulting to all tenants/log types."""
    conn = await database.get_db(DB_PATH)
    try:
        await database.set_setting(conn, "default_fetch_config", body.model_dump_json())
        return {"ok": True}
    finally:
        await conn.close()


# ── Fetch schedules (recurring pulls) ─────────────────────────────────────────
@app.get("/api/schedules")
async def list_schedules():
    conn = await database.get_db(DB_PATH)
    try:
        schedules = await database.get_schedules(conn)
        for s in schedules:
            s["tenants"]   = json.loads(s["tenants"])
            s["log_types"] = json.loads(s["log_types"])
        return schedules
    finally:
        await conn.close()


@app.post("/api/schedules", status_code=201)
async def create_schedule(body: ScheduleRequest):
    conn = await database.get_db(DB_PATH)
    try:
        schedule_id = str(uuid.uuid4())
        await database.create_schedule(
            conn, schedule_id, body.name,
            json.dumps(body.tenants), json.dumps(body.log_types),
            body.hours, body.interval_minutes, body.enabled,
        )
        return {"ok": True, "id": schedule_id}
    finally:
        await conn.close()


@app.put("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleRequest):
    conn = await database.get_db(DB_PATH)
    try:
        existing = await database.get_schedule(conn, schedule_id)
        if not existing:
            raise HTTPException(404, "Schedule not found")
        await database.update_schedule(
            conn, schedule_id, body.name,
            json.dumps(body.tenants), json.dumps(body.log_types),
            body.hours, body.interval_minutes, body.enabled,
        )
        return {"ok": True}
    finally:
        await conn.close()


@app.delete("/api/schedules/{schedule_id}")
async def remove_schedule(schedule_id: str):
    conn = await database.get_db(DB_PATH)
    try:
        await database.delete_schedule(conn, schedule_id)
        return {"ok": True}
    finally:
        await conn.close()


@app.get("/api/fetch/stream")
async def fetch_stream():
    """SSE stream — attach to the active job from any page/tab."""
    global _active_job

    async def event_stream() -> AsyncGenerator[str, None]:
        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        job = _active_job
        if not job:
            yield sse({"type": "idle"})
            return

        # Send current snapshot immediately so late subscribers are up-to-date
        yield sse({
            "type":         "snapshot",
            "job_id":       job.id,
            "status":       job.status,
            "status_msg":   job.status_msg,
            "done":         job.done,
            "total":        job.total,
            "current_file": job.current_file,
            "current_tenant":   job.current_tenant,
            "current_log_type": job.current_log_type,
            "imported":     job.imported,
            "error_msg":    job.error_msg,
        })

        if job.status != "running":
            return

        q = job.attach()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    yield sse(event)
                    if event.get("type") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            job.detach(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run_fetch(job: FetchJob, body: FetchRequest):
    """Background coroutine that does the actual downloading + importing."""
    conn = await database.get_db(DB_PATH)
    try:
        # Resolve tenants
        if "all" in body.tenants:
            tenants = await database.get_tenants(conn)
        else:
            tenants = [
                t for tid in body.tenants
                if (t := await database.get_tenant(conn, tid.lower())) is not None
            ]

        if not tenants:
            job.status    = "error"
            job.error_msg = "No tenants configured."
            job.push({"type": "error", "msg": job.error_msg})
            return

        log_types = body.log_types or ["trace", "http"]
        cutoff_ms = 0
        if body.hours > 0:
            cutoff_ms = int((time.time() - body.hours * 3600) * 1000)

        for tenant in tenants:
            if job.cancel_requested:
                break
            # One shared httpx client per tenant, reused for the token call,
            # file listing and every file download across all log_types —
            # avoids a fresh TCP/TLS handshake per request.
            client = cpi_api.make_client()
            try:
                for lt in log_types:
                    if job.cancel_requested:
                        break
                    job.current_tenant   = tenant["name"]
                    job.current_log_type = lt
                    job.status_msg = f"🔑 Requesting token for {tenant['name']}…"
                    job.push({"type": "status", "msg": job.status_msg})

                    if MOCK:
                        mock_files = list(MOCK_DIR.glob("*.log"))
                        job.total      = len(mock_files)
                        job.status_msg = f"{tenant['name']} · {lt}: {len(mock_files)} files"
                        job.push({"type": "files_found", "count": len(mock_files),
                                   "tenant": tenant["name"], "log_type": lt})
                        for i, mf in enumerate(mock_files, 1):
                            if job.cancel_requested:
                                break
                            already = (await database.get_file_import(conn, tenant["id"], mf.name))["lines"]
                            try:
                                newly_imported = await database.import_log_file(
                                    conn, tenant["id"], lt, mf, mf.name, already)
                            except Exception as e:
                                job.push({"type": "warn", "msg": f"Import failed: {mf.name}: {e}"})
                                newly_imported = 0
                            job.imported  += newly_imported
                            job.done       = i
                            job.current_file = mf.name
                            job.status_msg = f"{tenant['name']} · {lt}: {mf.name} ({i}/{len(mock_files)})"
                            job.push({"type": "progress", "done": i, "total": len(mock_files),
                                       "file": mf.name, "new_rows": newly_imported,
                                       "imported": job.imported})
                            await asyncio.sleep(0.05)
                    else:
                        try:
                            token = await cpi_api.get_token(
                                tenant["oauth_url"], tenant["client_id"], tenant["client_secret"], client
                            )
                        except Exception as e:
                            job.push({"type": "error", "msg": f"Couldn't get an OAuth token for {tenant['name']}: {e}"})
                            continue

                        try:
                            files = await cpi_api.list_remote_files(tenant["api_url"], token, lt, client)
                        except Exception as e:
                            job.push({"type": "error", "msg": f"Couldn't list log files for {tenant['name']}: {e}"})
                            continue

                        if cutoff_ms > 0:
                            files = [f for f in files if cpi_api.epoch_ms(f.get("LastModified", "")) > cutoff_ms]

                        job.total      = len(files)
                        job.status_msg = f"{tenant['name']} · {lt}: {len(files)} files"
                        job.push({"type": "files_found", "count": len(files),
                                   "tenant": tenant["name"], "log_type": lt})

                        tenant_log_dir = LOGS_DIR / tenant["id"]
                        tenant_log_dir.mkdir(parents=True, exist_ok=True)

                        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
                        counters  = {"done": 0, "imported": job.imported}

                        async def process_file(f):
                            async with semaphore:
                                if job.cancel_requested:
                                    return
                                # The file name comes from the remote server; never let it
                                # point outside this tenant's log directory.
                                name = f["Name"]
                                if not name or Path(name).name != name or name in (".", ".."):
                                    counters["done"] += 1
                                    job.done = counters["done"]
                                    job.push({"type": "warn", "msg": f"Skipped file with unsafe name: {name!r}"})
                                    return
                                dest = tenant_log_dir / name
                                remote_size = int(f.get("Size", 0))
                                file_import = await database.get_file_import(conn, tenant["id"], f["Name"])

                                # Backfill size from local file if missing (legacy imports)
                                if file_import["lines"] > 0 and file_import["size"] == 0 and dest.exists():
                                    local_size = dest.stat().st_size
                                    await database.update_file_import_size(conn, tenant["id"], f["Name"], local_size)
                                    file_import["size"] = local_size

                                counters["done"] += 1
                                i = counters["done"]
                                job.done         = i
                                job.current_file = f["Name"]
                                job.status_msg   = f"{tenant['name']} · {lt}: {f['Name']} ({i}/{len(files)})"

                                # Skip if already fully imported
                                if file_import["size"] == remote_size and file_import["lines"] > 0:
                                    job.push({"type": "progress", "done": i, "total": len(files),
                                               "file": f["Name"], "new_rows": 0, "imported": counters["imported"]})
                                    return

                                # Download if new or grown
                                if not dest.exists() or dest.stat().st_size != remote_size:
                                    try:
                                        # Streamed to disk as gzip with constant memory; the
                                        # parser auto-detects gzip via magic bytes.
                                        await cpi_api.download_to_file(
                                            tenant["api_url"], token, f["Name"], f["Application"],
                                            client, dest,
                                        )
                                    except Exception as e:
                                        job.push({"type": "warn", "msg": f"Download failed for {f['Name']}: {e}"})
                                        job.push({"type": "progress", "done": i, "total": len(files),
                                                   "file": f["Name"], "new_rows": 0, "imported": counters["imported"]})
                                        return

                                # A file that can't be read completely (e.g. truncated
                                # gzip) keeps the rows committed so far, is not marked
                                # as fully imported and is retried on the next fetch.
                                try:
                                    newly_imported = await database.import_log_file(
                                        conn, tenant["id"], lt, dest, f["Name"],
                                        file_import["lines"], remote_size)
                                except Exception as e:
                                    job.push({"type": "warn", "msg": f"Import failed: {f['Name']}: {e}"})
                                    newly_imported = 0
                                counters["imported"] += newly_imported
                                job.imported          = counters["imported"]

                                job.push({"type": "progress", "done": i, "total": len(files),
                                           "file": f["Name"], "new_rows": newly_imported,
                                           "imported": counters["imported"]})

                        await asyncio.gather(*[process_file(f) for f in files])
            finally:
                await client.aclose()

        if job.cancel_requested:
            job.status     = "cancelled"
            job.status_msg = "Cancelled"
            job.push({"type": "cancelled", "imported": job.imported})
        else:
            job.status     = "done"
            job.status_msg = "Completed"
            job.push({"type": "done", "imported": job.imported})

    except Exception as e:
        job.status    = "error"
        job.error_msg = str(e)
        job.push({"type": "error", "msg": str(e)})
    finally:
        await conn.close()


# ── LLM query API ─────────────────────────────────────────────────────────────

class LLMQueryRequest(BaseModel):
    tenant:    Optional[str] = None   # tenant id, or null for all
    level:     Optional[str] = None   # ERROR | WARN | INFO | DEBUG
    iflow:     Optional[str] = None   # partial iflow name match
    grep:      Optional[str] = None   # full-text search in message/logger
    date_from: Optional[str] = None   # ISO datetime, e.g. "2024-01-01 00:00:00"
    date_to:   Optional[str] = None   # ISO datetime, e.g. "2024-01-31 23:59:59"
    limit:     int = 50               # max log entries returned (1–200)

@app.get("/api/query/schema")
async def llm_query_schema():
    """Describes the query API for LLM tool-use / function-calling."""
    conn = await database.get_db(DB_PATH)
    try:
        tenants = await database.get_tenants(conn)
    finally:
        await conn.close()

    return {
        "description": (
            "CPI Log Lens query API. Use POST /api/query to search SAP CPI log entries. "
            "Use GET /api/stats to get aggregated statistics."
        ),
        "endpoints": {
            "POST /api/query": {
                "description": "Search log entries with optional filters.",
                "body": {
                    "tenant":    "string | null — tenant id to filter; null means all tenants",
                    "level":     "string | null — log level: ERROR, WARN, INFO, DEBUG",
                    "iflow":     "string | null — partial iflow name (case-insensitive LIKE match)",
                    "grep":      "string | null — full-text search in message and logger fields",
                    "date_from": "string | null — start datetime 'YYYY-MM-DD HH:MM:SS'",
                    "date_to":   "string | null — end datetime 'YYYY-MM-DD HH:MM:SS'",
                    "limit":     "integer 1–200 — max entries to return (default 50)",
                },
                "response": {
                    "total_matching": "total rows matching the filter (may exceed limit)",
                    "returned":       "number of rows returned",
                    "summary":        "short natural-language summary of the result",
                    "items":          "array of log entries",
                    "item_fields":    "id, tenant, log_type, filename, timestamp, level, logger, iflow, message, ip, node",
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
    conn = await database.get_db(DB_PATH)
    try:
        result = await database.query_logs(
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
    finally:
        await conn.close()

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
        summary += f" Use a stricter filter or increase limit (max 200) to see more."

    return {
        "total_matching": total,
        "returned": len(items),
        "summary": summary,
        "items": items,
    }


# ── Log query ─────────────────────────────────────────────────────────────────
@app.get("/api/logs")
async def get_logs(
    tenant:    Optional[str] = None,
    level:     Optional[str] = None,
    iflow:     Optional[str] = None,
    grep:      Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    page:      int = Query(1,   ge=1, le=1_000_000),
    page_size: int = Query(100, ge=1, le=500),
):
    _check_datetime(date_from, "date_from")
    _check_datetime(date_to, "date_to")
    conn = await database.get_db(DB_PATH)
    try:
        return await database.query_logs(
            conn,
            tenant=tenant, level=level, iflow=iflow, grep=grep,
            date_from=date_from, date_to=date_to,
            page=page, page_size=page_size,
        )
    finally:
        await conn.close()


@app.get("/api/logs/{entry_id}")
async def get_log_entry(entry_id: int):
    conn = await database.get_db(DB_PATH)
    try:
        entry = await database.get_log_entry(conn, entry_id)
        if not entry:
            raise HTTPException(404, "Entry not found")
        return entry
    finally:
        await conn.close()


# ── Stats ─────────────────────────────────────────────────────────────────────
@app.get("/api/stats")
async def get_stats(tenant: Optional[str] = None):
    conn = await database.get_db(DB_PATH)
    try:
        return await database.get_stats(conn, tenant)
    finally:
        await conn.close()


# ── DB info ───────────────────────────────────────────────────────────────────
@app.get("/api/db/info")
async def db_info():
    conn = await database.get_db(DB_PATH)
    try:
        return await database.get_db_info(conn, DB_PATH)
    finally:
        await conn.close()


@app.post("/api/db/clear")
async def db_clear():
    conn = await database.get_db(DB_PATH)
    try:
        await database.clear_db(conn)
        return {"ok": True}
    finally:
        await conn.close()


class CleanupRequest(BaseModel):
    older_than_days: int = Field(ge=1, le=36500)  # delete entries older than this many days
    tenant: Optional[str] = None  # optional: restrict to one tenant

@app.post("/api/db/cleanup")
async def db_cleanup(req: CleanupRequest):
    """Delete log entries older than N days, optionally filtered by tenant."""
    conn = await database.get_db(DB_PATH)
    try:
        result = await database.cleanup_old_logs(conn, req.older_than_days, req.tenant)
        return {"ok": True, **result}
    finally:
        await conn.close()


# ── Serve frontend ────────────────────────────────────────────────────────────
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
else:
    log.warning("frontend directory %s not found; serving the API only", FRONTEND)

