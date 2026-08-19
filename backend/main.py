"""
CPI Log Explorer — FastAPI Backend
Serves the frontend and provides REST + SSE API.
"""
import asyncio
import json
import os
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
DB_PATH   = Path(os.getenv("DB_PATH",   "cpi_logs.db"))
LOGS_DIR  = Path(os.getenv("LOGS_DIR",  "logs"))
MOCK      = os.getenv("MOCK", "false").lower() == "true"
MOCK_DIR  = Path(__file__).parent / "mock"
FRONTEND  = Path(os.getenv("FRONTEND_DIR", str(Path(__file__).parent.parent / "frontend")))

database.DB_PATH = DB_PATH

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="CPI Log Explorer", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    await database.init_db(DB_PATH)
    await _load_tenants_from_json()


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
    tenant:    str = "all"   # "all" or specific tenant id
    log_type:  str = "trace" # "trace" | "http" | "both"
    hours:     int = 24      # 0 = no filter (all available)


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
        # Keep existing secret if masked value submitted
        secret = body.client_secret
        if set(secret) == {"•"}:
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


# ── Fetch (SSE) ───────────────────────────────────────────────────────────────
@app.post("/api/fetch")
async def fetch_logs(body: FetchRequest):
    """Start a log fetch. Returns Server-Sent Events stream with progress."""

    async def event_stream() -> AsyncGenerator[str, None]:
        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        conn = await database.get_db(DB_PATH)
        try:
            if body.tenant == "all":
                tenants = await database.get_tenants(conn)
            else:
                t = await database.get_tenant(conn, body.tenant)
                tenants = [t] if t else []

            if not tenants:
                yield sse({"type": "error", "msg": "No tenants configured"})
                return

            log_types = ["trace", "http"] if body.log_type == "both" else [body.log_type]
            cutoff_ms = 0
            if body.hours > 0:
                import time
                cutoff_ms = int((time.time() - body.hours * 3600) * 1000)

            total_imported = 0

            for tenant in tenants:
                for lt in log_types:
                    yield sse({"type": "status", "msg": f"🔑 Token für {tenant['name']} …"})

                    if MOCK:
                        # Mock mode: import local sample files
                        mock_files = list(MOCK_DIR.glob("*.log"))
                        yield sse({"type": "files_found", "count": len(mock_files), "tenant": tenant["name"], "log_type": lt})
                        for i, mf in enumerate(mock_files, 1):
                            if not await database.file_already_imported(conn, tenant["id"], mf.name):
                                rows = database.parse_log_file(tenant["id"], lt, mf)
                                n = await database.import_rows(conn, rows)
                                total_imported += n
                            yield sse({"type": "progress", "done": i, "total": len(mock_files), "file": mf.name})
                            await asyncio.sleep(0.05)
                    else:
                        try:
                            token = await cpi_api.get_token(
                                tenant["oauth_url"], tenant["client_id"], tenant["client_secret"]
                            )
                        except Exception as e:
                            yield sse({"type": "error", "msg": f"Token-Fehler für {tenant['name']}: {e}"})
                            continue

                        try:
                            files = await cpi_api.list_remote_files(tenant["api_url"], token, lt)
                        except Exception as e:
                            yield sse({"type": "error", "msg": f"Fehler beim Abrufen der Dateiliste: {e}"})
                            continue

                        # Filter by cutoff
                        if cutoff_ms > 0:
                            files = [f for f in files if cpi_api.epoch_ms(f.get("LastModified", "")) > cutoff_ms]

                        yield sse({"type": "files_found", "count": len(files), "tenant": tenant["name"], "log_type": lt})

                        tenant_log_dir = LOGS_DIR / tenant["id"]
                        tenant_log_dir.mkdir(parents=True, exist_ok=True)

                        for i, f in enumerate(files, 1):
                            dest = tenant_log_dir / f["Name"]
                            yield sse({"type": "progress", "done": i, "total": len(files), "file": f["Name"]})

                            if not dest.exists():
                                try:
                                    content = await cpi_api.download_file(
                                        tenant["api_url"], token, f["Name"], f["Application"]
                                    )
                                    dest.write_bytes(content)
                                except Exception as e:
                                    yield sse({"type": "warn", "msg": f"Download fehlgeschlagen: {f['Name']}: {e}"})
                                    continue

                            if not await database.file_already_imported(conn, tenant["id"], f["Name"]):
                                rows = database.parse_log_file(tenant["id"], lt, dest)
                                n = await database.import_rows(conn, rows)
                                total_imported += n

            yield sse({"type": "done", "imported": total_imported})
        finally:
            await conn.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# ── Serve frontend ────────────────────────────────────────────────────────────
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
