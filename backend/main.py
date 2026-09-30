"""
CPI Log Explorer — FastAPI Backend
Serves the frontend and provides REST + SSE API.
"""
import asyncio
import gzip
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db as database
import api as cpi_api

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
app = FastAPI(title="CPI Log Explorer", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_js(request, call_next):
    """Prevent browser caching of app.js during development."""
    response = await call_next(request)
    if request.url.path.endswith(".js"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


SCHEDULE_CHECK_SECONDS = int(os.getenv("SCHEDULE_CHECK_SECONDS", "60"))


@app.on_event("startup")
async def startup():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    await database.init_db(DB_PATH)
    await _load_tenants_from_json()
    if RETENTION_DAYS > 0:
        asyncio.create_task(_retention_loop())
    asyncio.create_task(_schedule_loop())


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
                print(f"[retention] Deleted {result['deleted']} log entries "
                      f"older than {RETENTION_DAYS} days ({result['remaining']} remaining).")
        except Exception as e:
            print(f"[retention] Cleanup failed: {e}")
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
                    print(f"[schedule] Starting '{sched['name']}' "
                          f"(tenants={body.tenants}, log_types={body.log_types}, hours={body.hours})")
                    asyncio.create_task(_run_fetch(job, body))
                    break  # one job at a time — remaining due schedules wait for next tick
        except Exception as e:
            print(f"[schedule] Loop error: {e}")
        await asyncio.sleep(SCHEDULE_CHECK_SECONDS)


async def _load_tenants_from_json():
    """Load tenants from TENANTS_CONFIG JSON file (default: /config/tenants.json)."""
    config_path = Path(os.getenv("TENANTS_CONFIG", "/config/tenants.jsonc"))
    if not config_path.exists():
        return

    try:
        import json5
        data = json5.loads(config_path.read_text())
        tenant_list = data.get("tenants", [])
    except Exception as e:
        print(f"[warn] Could not parse {config_path}: {e}")
        return

    conn = await database.get_db(DB_PATH)
    try:
        for t in tenant_list:
            tid    = t.get("id", "").strip().lower()
            name   = t.get("name", tid.upper())
            api    = t.get("api_url", "")
            oauth  = t.get("oauth_url", "")
            cid    = t.get("client_id", "")
            secret = t.get("client_secret", "")
            if tid and api and cid:
                await database.upsert_tenant(conn, tid, name, api, oauth, cid, secret)
    finally:
        await conn.close()


# ── Pydantic models ───────────────────────────────────────────────────────────
class TenantCreate(BaseModel):
    id:            str
    name:          str
    api_url:       str
    oauth_url:     str
    client_id:     str
    client_secret: str


class FetchRequest(BaseModel):
    tenants:   list[str] = ["all"]  # ["all"] or list of tenant ids
    log_types: list[str] = ["trace", "http"]
    hours:     int = 24      # 0 = no filter (all available)


class ScheduleRequest(BaseModel):
    name:             str
    tenants:          list[str] = ["all"]
    log_types:        list[str] = ["trace", "http"]
    hours:            int = 1          # time range pulled on every run
    interval_minutes: int = 15         # how often to run
    enabled:          bool = True


class DefaultFetchConfig(BaseModel):
    tenants:   list[str] = ["all"]
    log_types: list[str] = ["trace", "http"]
    hours:     int = 24


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
        return {"ok": False, "error": "Ein Download läuft bereits", "job_id": _active_job.id}

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
        return {"ok": False, "error": "Kein Job läuft aktuell"}
    _active_job.cancel_requested = True
    _active_job.push({"type": "status", "msg": "Abbruch angefordert …"})
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
            job.error_msg = "Keine Tenants konfiguriert"
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
                    job.status_msg = f"🔑 Token für {tenant['name']} …"
                    job.push({"type": "status", "msg": job.status_msg})

                    if MOCK:
                        mock_files = list(MOCK_DIR.glob("*.log"))
                        job.total      = len(mock_files)
                        job.status_msg = f"{tenant['name']} / {lt}: {len(mock_files)} Dateien"
                        job.push({"type": "files_found", "count": len(mock_files),
                                   "tenant": tenant["name"], "log_type": lt})
                        for i, mf in enumerate(mock_files, 1):
                            if job.cancel_requested:
                                break
                            already = (await database.get_file_import(conn, tenant["id"], mf.name))["lines"]
                            rows, unparsed = database.parse_log_file(tenant["id"], lt, mf)
                            new_rows = rows[already:]
                            newly_imported = await database.import_rows(conn, new_rows, tenant["id"], mf.name, len(rows)) if new_rows else 0
                            await database.import_unparsed_lines(conn, unparsed, tenant["id"], lt, mf.name)
                            job.imported  += newly_imported
                            job.done       = i
                            job.current_file = mf.name
                            job.status_msg = f"{tenant['name']} / {lt}: {mf.name} ({i}/{len(mock_files)})"
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
                            job.push({"type": "error", "msg": f"Token-Fehler für {tenant['name']}: {e}"})
                            continue

                        try:
                            files = await cpi_api.list_remote_files(tenant["api_url"], token, lt, client)
                        except Exception as e:
                            job.push({"type": "error", "msg": f"Fehler beim Abrufen der Dateiliste: {e}"})
                            continue

                        if cutoff_ms > 0:
                            files = [f for f in files if cpi_api.epoch_ms(f.get("LastModified", "")) > cutoff_ms]

                        job.total      = len(files)
                        job.status_msg = f"{tenant['name']} / {lt}: {len(files)} Dateien"
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
                                dest        = tenant_log_dir / f["Name"]
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
                                job.status_msg   = f"{tenant['name']} / {lt}: {f['Name']} ({i}/{len(files)})"

                                # Skip if already fully imported
                                if file_import["size"] == remote_size and file_import["lines"] > 0:
                                    job.push({"type": "progress", "done": i, "total": len(files),
                                               "file": f["Name"], "new_rows": 0, "imported": counters["imported"]})
                                    return

                                # Download if new or grown
                                if not dest.exists() or dest.stat().st_size != remote_size:
                                    try:
                                        content = await cpi_api.download_file(
                                            tenant["api_url"], token, f["Name"], f["Application"], client
                                        )
                                        # CPI's LogFiles $value endpoint decompresses server-side
                                        # before streaming even though the filename/Content-Type say
                                        # .gz (~30-40x larger than the announced Size). Re-compress
                                        # before writing to disk to actually save space — raw log
                                        # files must be kept (per requirement) but don't need to stay
                                        # as plaintext. parse_log_file() already auto-detects gzip via
                                        # magic bytes, so reading is unaffected.
                                        if not content.startswith(b"\x1f\x8b"):
                                            content = await asyncio.to_thread(gzip.compress, content)
                                        await asyncio.to_thread(dest.write_bytes, content)
                                    except Exception as e:
                                        job.push({"type": "warn", "msg": f"Download fehlgeschlagen: {f['Name']}: {e}"})
                                        job.push({"type": "progress", "done": i, "total": len(files),
                                                   "file": f["Name"], "new_rows": 0, "imported": counters["imported"]})
                                        return

                                rows, unparsed = database.parse_log_file(tenant["id"], lt, dest)
                                new_rows  = rows[file_import["lines"]:]
                                newly_imported = await database.import_rows(conn, new_rows, tenant["id"], f["Name"], len(rows), remote_size) if new_rows else 0
                                await database.import_unparsed_lines(conn, unparsed, tenant["id"], lt, f["Name"])
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
            job.status_msg = "Abgebrochen"
            job.push({"type": "cancelled", "imported": job.imported})
        else:
            job.status     = "done"
            job.status_msg = "Abgeschlossen"
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
    page:      int = Query(1,   ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
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
    older_than_days: int   # delete entries with timestamp older than this many days
    tenant: Optional[str] = None  # optional: restrict to one tenant

@app.post("/api/db/cleanup")
async def db_cleanup(req: CleanupRequest):
    """Delete log entries older than N days, optionally filtered by tenant."""
    if req.older_than_days < 1:
        raise HTTPException(400, "older_than_days must be >= 1")
    conn = await database.get_db(DB_PATH)
    try:
        result = await database.cleanup_old_logs(conn, req.older_than_days, req.tenant)
        return {"ok": True, **result}
    finally:
        await conn.close()


# ── Serve frontend ────────────────────────────────────────────────────────────
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")

