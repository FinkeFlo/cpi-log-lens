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

from app.config import BACKEND_DIR, get_settings
from app.cpi import client as cpi_api
from app.repositories import file_imports as file_imports_repo
from app.repositories import tenants as tenants_repo
from app.repositories.database import Database
from app.services import importer
from app.services import tenants as tenant_service
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


async def run_fetch(conn: Database, job: FetchJob, body: FetchParams) -> None:
    """Background coroutine that does the actual downloading + importing."""
    settings = get_settings()
    try:
        # Resolve tenants
        if "all" in body.tenants:
            tenants = await tenants_repo.get_tenants(conn)
        else:
            tenants = [t for tid in body.tenants if (t := await tenants_repo.get_tenant(conn, tid.lower())) is not None]

        if not tenants:
            job.status = "error"
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
                    job.current_tenant = tenant["name"]
                    job.current_log_type = lt
                    job.status_msg = f"🔑 Requesting token for {tenant['name']}…"
                    job.push({"type": "status", "msg": job.status_msg})

                    if settings.mock or tenant_service.is_demo(tenant):
                        mock_files = list(MOCK_DIR.glob("*.log"))
                        job.total = len(mock_files)
                        job.status_msg = f"{tenant['name']} · {lt}: {len(mock_files)} files"
                        job.push(
                            {"type": "files_found", "count": len(mock_files), "tenant": tenant["name"], "log_type": lt}
                        )
                        for i, mf in enumerate(mock_files, 1):
                            if job.cancel_requested:
                                break
                            already = (await file_imports_repo.get_file_import(conn, tenant["id"], mf.name))["lines"]
                            try:
                                newly_imported = await importer.import_log_file(
                                    conn, tenant["id"], lt, mf, mf.name, already
                                )
                            except Exception as e:
                                job.push({"type": "warn", "msg": f"Import failed: {mf.name}: {e}"})
                                newly_imported = 0
                            job.imported += newly_imported
                            job.done = i
                            job.current_file = mf.name
                            job.status_msg = f"{tenant['name']} · {lt}: {mf.name} ({i}/{len(mock_files)})"
                            job.push(
                                {
                                    "type": "progress",
                                    "done": i,
                                    "total": len(mock_files),
                                    "file": mf.name,
                                    "new_rows": newly_imported,
                                    "imported": job.imported,
                                }
                            )
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

                        job.total = len(files)
                        job.status_msg = f"{tenant['name']} · {lt}: {len(files)} files"
                        job.push({"type": "files_found", "count": len(files), "tenant": tenant["name"], "log_type": lt})

                        tenant_log_dir = settings.logs_dir / tenant["id"]
                        tenant_log_dir.mkdir(parents=True, exist_ok=True)

                        semaphore = asyncio.Semaphore(settings.fetch_concurrency)
                        counters = {"done": 0, "imported": job.imported}

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
                                file_import = await file_imports_repo.get_file_import(conn, tenant["id"], f["Name"])

                                # Backfill size from local file if missing (legacy imports)
                                if file_import["lines"] > 0 and file_import["size"] == 0 and dest.exists():
                                    local_size = dest.stat().st_size
                                    await file_imports_repo.update_file_import_size(
                                        conn, tenant["id"], f["Name"], local_size
                                    )
                                    file_import["size"] = local_size

                                counters["done"] += 1
                                i = counters["done"]
                                job.done = i
                                job.current_file = f["Name"]
                                job.status_msg = f"{tenant['name']} · {lt}: {f['Name']} ({i}/{len(files)})"

                                # Skip if already fully imported
                                if file_import["size"] == remote_size and file_import["lines"] > 0:
                                    job.push(
                                        {
                                            "type": "progress",
                                            "done": i,
                                            "total": len(files),
                                            "file": f["Name"],
                                            "new_rows": 0,
                                            "imported": counters["imported"],
                                        }
                                    )
                                    return

                                # Download if new or grown
                                if not dest.exists() or dest.stat().st_size != remote_size:
                                    try:
                                        # Streamed to disk as gzip with constant memory; the
                                        # parser auto-detects gzip via magic bytes.
                                        await cpi_api.download_to_file(
                                            tenant["api_url"],
                                            token,
                                            f["Name"],
                                            f["Application"],
                                            client,
                                            dest,
                                        )
                                    except Exception as e:
                                        job.push({"type": "warn", "msg": f"Download failed for {f['Name']}: {e}"})
                                        job.push(
                                            {
                                                "type": "progress",
                                                "done": i,
                                                "total": len(files),
                                                "file": f["Name"],
                                                "new_rows": 0,
                                                "imported": counters["imported"],
                                            }
                                        )
                                        return

                                # A file that can't be read completely (e.g. truncated
                                # gzip) keeps the rows committed so far, is not marked
                                # as fully imported and is retried on the next fetch.
                                try:
                                    newly_imported = await importer.import_log_file(
                                        conn, tenant["id"], lt, dest, f["Name"], file_import["lines"], remote_size
                                    )
                                except Exception as e:
                                    job.push({"type": "warn", "msg": f"Import failed: {f['Name']}: {e}"})
                                    newly_imported = 0
                                counters["imported"] += newly_imported
                                job.imported = counters["imported"]

                                job.push(
                                    {
                                        "type": "progress",
                                        "done": i,
                                        "total": len(files),
                                        "file": f["Name"],
                                        "new_rows": newly_imported,
                                        "imported": counters["imported"],
                                    }
                                )

                        await asyncio.gather(*[process_file(f) for f in files])
            finally:
                await client.aclose()

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
