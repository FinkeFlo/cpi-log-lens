"""The fetch job: download log files from CPI and import them, one job at a time,
with progress events for SSE subscribers."""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

from app import cpi
from app.config import BACKEND_DIR, get_settings
from app.cpi.client import CpiClient, RemoteLogFile
from app.repositories import file_imports as file_imports_repo
from app.repositories import tenants as tenants_repo
from app.repositories.database import Database
from app.services import importer
from app.tasks import spawn

log = logging.getLogger("cpi")

# Bundled sample logs, imported instead of calling CPI in MOCK mode and for the demo tenant.
MOCK_DIR = BACKEND_DIR / "mock"


@dataclass(frozen=True)
class FetchParams:
    tenants: list[str]  # ["all"] or tenant ids
    log_types: list[str]
    hours: int  # 0 = all available files


@dataclass
class FetchJob:
    id: str
    status: str = "running"  # running | done | error | cancelled
    status_msg: str = ""
    done: int = 0
    total: int = 0
    current_file: str = ""
    current_tenant: str = ""
    current_log_type: str = ""
    imported: int = 0
    error_msg: str = ""
    cancel_requested: bool = False
    # Listeners waiting for new events (one queue per SSE subscriber)
    _listeners: list = field(default_factory=list)

    def push(self, event: dict):
        """Broadcast an event to all active SSE listeners."""
        if event.get("type") in ("warn", "error", "done", "cancelled"):
            level = logging.INFO if event["type"] in ("done", "cancelled") else logging.WARNING
            log.log(
                level,
                "fetch job %s: %s %s",
                self.id[:8],
                event["type"],
                event.get("msg", f"imported={event.get('imported')}"),
                extra={"fields": {"job_id": self.id, "event": event["type"]}},
            )
        for q in list(self._listeners):
            q.put_nowait(event)

    def attach(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.append(q)
        return q

    def detach(self, q: asyncio.Queue):
        with contextlib.suppress(ValueError):
            self._listeners.remove(q)

    def snapshot(self) -> dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "status_msg": self.status_msg,
            "done": self.done,
            "total": self.total,
            "current_file": self.current_file,
            "current_tenant": self.current_tenant,
            "current_log_type": self.current_log_type,
            "imported": self.imported,
            "error_msg": self.error_msg,
        }


# Single active job (only one fetch at a time)
active_job: FetchJob | None = None


def is_running() -> bool:
    return active_job is not None and active_job.status == "running"


def start(db: Database, params: FetchParams) -> FetchJob | None:
    """Start a fetch job in the background; None if one is already running."""
    global active_job
    if is_running():
        return None
    job = FetchJob(id=str(uuid.uuid4()))
    active_job = job
    spawn(run_fetch(db, job, params))
    return job


def request_cancel() -> FetchJob | None:
    """Ask the running job to stop at the next file boundary (the file in flight
    is finished, so the database stays consistent); None if no job is running."""
    job = active_job
    if job is None or job.status != "running":
        return None
    job.cancel_requested = True
    job.push({"type": "status", "msg": "Cancelling…"})
    return job


def mark_cancelled_for_shutdown() -> None:
    job = active_job
    if job and job.status == "running":
        log.info("shutdown: cancelling fetch job %s", job.id[:8])
        job.cancel_requested = True
        job.status = "cancelled"
        job.status_msg = "Cancelled (shutdown)"


async def event_stream() -> AsyncGenerator[str, None]:
    """SSE events of the active job: a snapshot, then live events until it ends."""

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    job = active_job
    if not job:
        yield sse({"type": "idle"})
        return

    # Send current snapshot immediately so late subscribers are up-to-date
    yield sse({"type": "snapshot", **job.snapshot()})

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
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        job.detach(q)


async def run_fetch(db: Database, job: FetchJob, params: FetchParams) -> None:
    """Background coroutine that does the actual downloading + importing."""
    try:
        # Resolve tenants
        if "all" in params.tenants:
            tenants = await tenants_repo.get_tenants(db)
        else:
            tenants = [t for tid in params.tenants if (t := await tenants_repo.get_tenant(db, tid.lower())) is not None]

        if not tenants:
            job.status = "error"
            job.error_msg = "No tenants configured."
            job.push({"type": "error", "msg": job.error_msg})
            return

        log_types = params.log_types or ["trace", "http"]
        cutoff_ms = int((time.time() - params.hours * 3600) * 1000) if params.hours > 0 else 0

        for tenant in tenants:
            if job.cancel_requested:
                break
            if get_settings().mock or cpi.is_demo(tenant):
                for log_type in log_types:
                    if job.cancel_requested:
                        break
                    await _import_samples(db, job, tenant, log_type)
                continue
            async with cpi.client_for(tenant) as client:
                for log_type in log_types:
                    if job.cancel_requested:
                        break
                    await _fetch_log_type(_TenantFetch(db, job, client, tenant, log_type), cutoff_ms)

        if job.cancel_requested:
            job.status = "cancelled"
            job.status_msg = "Cancelled"
            job.push({"type": "cancelled", "imported": job.imported})
        else:
            job.status = "done"
            job.status_msg = "Completed"
            job.push({"type": "done", "imported": job.imported})

    except Exception as e:
        job.status = "error"
        job.error_msg = str(e)
        job.push({"type": "error", "msg": str(e)})


@dataclass
class _TenantFetch:
    """State of fetching one log type of one tenant."""

    db: Database
    job: FetchJob
    client: CpiClient
    tenant: dict
    log_type: str
    files_total: int = 0
    files_done: int = 0

    @property
    def tenant_id(self) -> str:
        return self.tenant["id"]

    def progress(self, file: str, new_rows: int) -> None:
        self.job.push(
            {
                "type": "progress",
                "done": self.files_done,
                "total": self.files_total,
                "file": file,
                "new_rows": new_rows,
                "imported": self.job.imported,
            }
        )


async def _fetch_log_type(ctx: _TenantFetch, cutoff_ms: int) -> None:
    job, name = ctx.job, ctx.tenant["name"]
    job.current_tenant = name
    job.current_log_type = ctx.log_type
    job.status_msg = f"🔑 Requesting token for {name}…"
    job.push({"type": "status", "msg": job.status_msg})
    try:
        await ctx.client.get_token()
    except Exception as e:
        job.push({"type": "error", "msg": f"Couldn't get an OAuth token for {name}: {e}"})
        return
    try:
        files = await ctx.client.list_files(ctx.log_type)
    except Exception as e:
        job.push({"type": "error", "msg": f"Couldn't list log files for {name}: {e}"})
        return
    if cutoff_ms > 0:
        files = [f for f in files if f.last_modified_ms > cutoff_ms]

    ctx.files_total = job.total = len(files)
    job.status_msg = f"{name} · {ctx.log_type}: {len(files)} files"
    job.push({"type": "files_found", "count": len(files), "tenant": name, "log_type": ctx.log_type})

    tenant_log_dir = get_settings().logs_dir / ctx.tenant_id
    tenant_log_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(get_settings().fetch_concurrency)

    async def bounded(f: RemoteLogFile) -> None:
        async with semaphore:
            if not job.cancel_requested:
                await _fetch_file(ctx, f, tenant_log_dir)

    await asyncio.gather(*[bounded(f) for f in files])


async def _fetch_file(ctx: _TenantFetch, f: RemoteLogFile, tenant_log_dir: Path) -> None:
    """Download one file if it is new or has grown, and import the new rows."""
    job = ctx.job
    # The file name comes from the remote server; never let it point outside
    # this tenant's log directory.
    if not f.name or Path(f.name).name != f.name or f.name in (".", ".."):
        ctx.files_done += 1
        job.done = ctx.files_done
        job.push({"type": "warn", "msg": f"Skipped file with unsafe name: {f.name!r}"})
        return
    dest = tenant_log_dir / f.name
    file_import = await file_imports_repo.get_file_import(ctx.db, ctx.tenant_id, f.name)

    # Backfill size from local file if missing (legacy imports)
    if file_import["lines"] > 0 and file_import["size"] == 0 and dest.exists():
        local_size = dest.stat().st_size
        await file_imports_repo.update_file_import_size(ctx.db, ctx.tenant_id, f.name, local_size)
        file_import["size"] = local_size

    ctx.files_done += 1
    job.done = ctx.files_done
    job.current_file = f.name
    job.status_msg = f"{ctx.tenant['name']} · {ctx.log_type}: {f.name} ({ctx.files_done}/{ctx.files_total})"

    # Skip if already fully imported
    if file_import["size"] == f.size and file_import["lines"] > 0:
        ctx.progress(f.name, 0)
        return

    # Download if new or grown
    if not dest.exists() or dest.stat().st_size != f.size:
        try:
            # Streamed to disk as gzip with constant memory; the parser
            # auto-detects gzip via magic bytes.
            await ctx.client.download(f, dest)
        except Exception as e:
            job.push({"type": "warn", "msg": f"Download failed for {f.name}: {e}"})
            ctx.progress(f.name, 0)
            return

    # A file that can't be read completely (e.g. truncated gzip) keeps the rows
    # committed so far, is not marked as fully imported and is retried on the
    # next fetch.
    try:
        new_rows = await importer.import_log_file(
            ctx.db, ctx.tenant_id, ctx.log_type, dest, f.name, file_import["lines"], f.size
        )
    except Exception as e:
        job.push({"type": "warn", "msg": f"Import failed: {f.name}: {e}"})
        new_rows = 0
    job.imported += new_rows
    ctx.progress(f.name, new_rows)


async def _import_samples(db: Database, job: FetchJob, tenant: dict, log_type: str) -> None:
    """MOCK mode and the demo tenant: import the bundled sample logs instead of calling CPI."""
    name = tenant["name"]
    job.current_tenant = name
    job.current_log_type = log_type
    mock_files = list(MOCK_DIR.glob("*.log"))
    job.total = len(mock_files)
    job.status_msg = f"{name} · {log_type}: {len(mock_files)} files"
    job.push({"type": "files_found", "count": len(mock_files), "tenant": name, "log_type": log_type})
    for i, mf in enumerate(mock_files, 1):
        if job.cancel_requested:
            break
        already = (await file_imports_repo.get_file_import(db, tenant["id"], mf.name))["lines"]
        try:
            new_rows = await importer.import_log_file(db, tenant["id"], log_type, mf, mf.name, already)
        except Exception as e:
            job.push({"type": "warn", "msg": f"Import failed: {mf.name}: {e}"})
            new_rows = 0
        job.imported += new_rows
        job.done = i
        job.current_file = mf.name
        job.status_msg = f"{name} · {log_type}: {mf.name} ({i}/{len(mock_files)})"
        job.push(
            {
                "type": "progress",
                "done": i,
                "total": len(mock_files),
                "file": mf.name,
                "new_rows": new_rows,
                "imported": job.imported,
            }
        )
        await asyncio.sleep(0.05)
