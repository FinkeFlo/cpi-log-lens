"""The fetch job: download log files from CPI and import them, one job at a time,
with progress events for SSE subscribers."""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app import cpi
from app.config import get_settings
from app.cpi.client import CpiClient, RemoteLogFile
from app.repositories import fetch_runs as fetch_runs_repo
from app.repositories import file_imports as file_imports_repo
from app.repositories import tenants as tenants_repo
from app.repositories.database import Database
from app.services import importer
from app.tasks import spawn

log = logging.getLogger("cpi")


@dataclass(frozen=True)
class FetchParams:
    tenants: list[str]  # ["all"] or tenant ids
    log_types: list[str]
    hours: int  # 0 = all available files


class JobAlreadyRunning(Exception):
    """Only one fetch job runs at a time."""

    def __init__(self, job: "FetchJob") -> None:
        super().__init__(f"fetch job {job.id} is already running")
        self.job = job


# Events a subscriber may fall behind by; older ones are dropped.
SSE_QUEUE_SIZE = 1000
# Warnings and per-tenant errors kept for the status and the UI; the counters include all.
MAX_PROBLEMS = 50
# Events after which a job has ended and its SSE stream closes.
FINAL_EVENTS = ("done", "error", "cancelled")
# Error of the parts a failed job did not reach.
NOT_FETCHED = "Not fetched: the job stopped with an error."
# How often the progress of a running job is written to its history entry.
PROGRESS_SAVE_SECONDS = 10.0


@dataclass
class FetchJob:
    id: str
    trigger: str = "manual"
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
    # Totals over all tenants and log types, for the run history.
    files_total: int = 0
    files_done: int = 0
    warnings: int = 0
    errors: int = 0
    last_error: str = ""
    recorded: bool = False  # final state written to the run history
    # One entry per tenant and log type, in fetch order: status (pending | running |
    # done | failed | cancelled), file counts, imported rows, warnings and the error.
    parts: list[dict] = field(default_factory=list)
    # The latest warnings and per-tenant errors (at most MAX_PROBLEMS).
    problems: list[dict] = field(default_factory=list)
    # Set once the job has ended and its run is recorded.
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    # Called with the final status before the job shows it (the service records the run).
    on_finish: Callable[["FetchJob", str], Awaitable[None]] | None = None
    # Listeners waiting for new events (one queue per SSE subscriber)
    _listeners: list = field(default_factory=list)

    def push(self, event: dict):
        """Broadcast an event to all active SSE listeners."""
        self._count(event)
        self._broadcast(event)

    async def finish(self, status: str, event: dict, *, status_msg: str = "", error: str = "") -> None:
        """End the job: record it, then show the final status and send the final event,
        so whoever sees the job ended also finds it in the run history."""
        if error:
            self.error_msg = error
        self._count(event)
        if self.on_finish is not None:
            await self.on_finish(self, status)
        self.status = status
        self.status_msg = status_msg
        self._broadcast(event)

    def _count(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "files_found":
            self.files_total += event["count"]
        elif kind == "progress":
            self.files_done += 1
        elif kind == "warn":
            self.warnings += 1
        elif kind in ("error", "tenant_error"):
            self.errors += 1
            self.last_error = event.get("msg", "")
        if kind in ("warn", "tenant_error"):
            part = event.get("part") or {}
            self.problems.append(
                {
                    "level": "error" if kind == "tenant_error" else "warn",
                    "tenant": part.get("tenant", ""),
                    "tenant_name": part.get("tenant_name", ""),
                    "log_type": part.get("log_type", ""),
                    "file": event.get("file", ""),
                    "msg": event.get("msg", ""),
                }
            )
            del self.problems[:-MAX_PROBLEMS]

    def _broadcast(self, event: dict) -> None:
        kind = event.get("type")
        if kind in ("warn", "tenant_error", "error", "done", "cancelled"):
            level = logging.INFO if kind in ("done", "cancelled") else logging.WARNING
            log.log(
                level,
                "fetch job %s: %s %s",
                self.id[:8],
                kind,
                event.get("msg", f"imported={event.get('imported')}"),
                extra={"fields": {"job_id": self.id, "event": kind}},
            )
        for q in list(self._listeners):
            if q.full():  # a subscriber that does not read: drop its oldest event
                q.get_nowait()
            q.put_nowait(event)

    def attach(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE_SIZE)
        self._listeners.append(q)
        return q

    def detach(self, q: asyncio.Queue):
        with contextlib.suppress(ValueError):
            self._listeners.remove(q)

    def set_parts(self, parts: list[tuple[str, str, str]]) -> None:
        """Announce what the job fetches: (tenant id, tenant name, log type) in order."""
        self.parts = [
            {
                "index": i,
                "tenant": tenant,
                "tenant_name": name,
                "log_type": log_type,
                "status": "pending",
                "files_total": None,  # known once the file list is read
                "files_done": 0,
                "imported": 0,
                "warnings": 0,
                "error": "",
            }
            for i, (tenant, name, log_type) in enumerate(parts)
        ]
        self.push({"type": "parts", "parts": [dict(p) for p in self.parts]})

    def update_part(self, index: int, **changes) -> dict:
        """Change a part; the returned copy goes into the event that reports the change."""
        part = self.parts[index]
        part.update(changes)
        return dict(part)

    def end_parts(self, status: str, error: str = "") -> None:
        """The job ends early (cancelled or failed): every part not finished gets `status`;
        on an error, the running part gets the error and the waiting ones a note."""
        for part in self.parts:
            if part["status"] not in ("pending", "running"):
                continue
            changes: dict = {"status": status}
            if error:
                changes["error"] = error if part["status"] == "running" else NOT_FETCHED
            self.push({"type": "part", "part": self.update_part(part["index"], **changes)})

    def summary(self) -> str:
        if self.errors:
            return "Completed with errors"
        if self.warnings:
            return "Completed with warnings"
        return "Completed"

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
            "files_total": self.files_total,
            "files_done": self.files_done,
            "warnings": self.warnings,
            "errors": self.errors,
            "parts": [dict(p) for p in self.parts],
            "problems": [dict(p) for p in self.problems],
        }

    def counters(self) -> dict:
        return {
            "files_total": self.files_total,
            "files_done": self.files_done,
            "rows_imported": self.imported,
            "warnings": self.warnings,
            "errors": self.errors,
            "error": self.error_msg or self.last_error or None,
        }


Runner = Callable[[Database, FetchJob, FetchParams], Awaitable[None]]


class FetchService:
    """Owns the fetch job: at most one runs at a time, whoever starts it (UI,
    API, scheduler). Starting is guarded by a lock, so two starts in the same
    moment cannot both pass the check. Every run is recorded in fetch_runs."""

    def __init__(self, db: Database, runner: Runner | None = None) -> None:
        self.db = db
        self._runner = runner or run_fetch
        self._lock = asyncio.Lock()
        self.job: FetchJob | None = None
        self._task: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self.job is not None and self.job.status == "running"

    async def start(self, params: FetchParams, trigger: str = "manual") -> FetchJob:
        """Start a fetch job in the background; JobAlreadyRunning if one runs."""
        async with self._lock:
            if self.is_running():
                assert self.job is not None
                raise JobAlreadyRunning(self.job)
            job = FetchJob(id=str(uuid.uuid4()), trigger=trigger, on_finish=self._finished)
            await fetch_runs_repo.insert_run(self.db, job.id, trigger, asdict(params), datetime.now(UTC))
            self.job = job
            self._task = spawn(self._run(job, params))
            return job

    async def _run(self, job: FetchJob, params: FetchParams) -> None:
        saver = asyncio.create_task(self._save_progress(job))
        try:
            await self._runner(self.db, job, params)
        finally:
            saver.cancel()
            await asyncio.gather(saver, return_exceptions=True)
            try:
                if not job.recorded:
                    # Cut off (shutdown) or ended without finish(): record the final state anyway.
                    status = job.status if job.status != "running" else "interrupted"
                    await asyncio.shield(self._finished(job, status))
            finally:
                job.ended.set()

    async def _finished(self, job: FetchJob, status: str) -> None:
        await self._save(job, status=status, finished_at=datetime.now(UTC))
        job.recorded = True

    async def _save_progress(self, job: FetchJob) -> None:
        while True:
            await asyncio.sleep(PROGRESS_SAVE_SECONDS)
            await self._save(job)

    async def _save(self, job: FetchJob, **fields) -> None:
        try:
            await fetch_runs_repo.update_run(self.db, job.id, **job.counters(), **fields)
        except Exception:
            log.exception("fetch job %s: could not save the run history", job.id[:8])

    def request_cancel(self) -> FetchJob | None:
        """Ask the running job to stop at the next file boundary (the file in flight
        is finished, so the database stays consistent); None if no job is running."""
        job = self.job
        if job is None or job.status != "running":
            return None
        job.cancel_requested = True
        job.push({"type": "status", "msg": "Cancelling…"})
        return job

    async def shutdown(self) -> None:
        """Stop a running job (app shutdown); its run is recorded as interrupted."""
        job, task = self.job, self._task
        if job is None or task is None or task.done():
            return
        log.info("shutdown: cancelling fetch job %s", job.id[:8])
        job.cancel_requested = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        job.status = "cancelled"
        job.status_msg = "Cancelled (shutdown)"

    async def event_stream(self) -> AsyncGenerator[str, None]:
        """SSE events of the current job: a snapshot, then live events until it ends."""

        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        job = self.job
        if not job:
            yield sse({"type": "idle"})
            return

        # Listen before sending the snapshot: events pushed while the client reads it
        # wait in the queue instead of being lost.
        q = job.attach() if job.status == "running" else None
        try:
            # Send current snapshot immediately so late subscribers are up-to-date
            yield sse({"type": "snapshot", **job.snapshot()})
            if q is None:
                return
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    yield sse(event)
                    if event.get("type") in FINAL_EVENTS:
                        break
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if q is not None:
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
            msg = "No tenants configured."
            await job.finish("error", {"type": "error", "msg": msg}, error=msg)
            return

        log_types = params.log_types or ["trace", "http"]
        cutoff_ms = int((time.time() - params.hours * 3600) * 1000) if params.hours > 0 else 0
        job.set_parts([(t["id"], t["name"], log_type) for t in tenants for log_type in log_types])

        index = 0
        for tenant in tenants:
            if job.cancel_requested:
                break
            async with cpi.client_for(tenant) as client:
                for log_type in log_types:
                    if job.cancel_requested:
                        break
                    await _fetch_log_type(_TenantFetch(db, job, client, tenant, log_type, index), cutoff_ms)
                    index += 1

        if job.cancel_requested:
            job.end_parts("cancelled")
            await job.finish("cancelled", _final_event(job, "cancelled"), status_msg="Cancelled")
        else:
            await job.finish("done", _final_event(job, "done"), status_msg=job.summary())

    except Exception as e:
        # An unexpected failure (database, file system): no part may stay "running".
        job.end_parts("failed", error=str(e))
        await job.finish("error", {"type": "error", "msg": str(e), "parts": [dict(p) for p in job.parts]}, error=str(e))


def _final_event(job: FetchJob, kind: str) -> dict:
    """The last event of a job, with its final parts, so a client that missed part
    updates still ends with the right state."""
    return {
        "type": kind,
        "imported": job.imported,
        "warnings": job.warnings,
        "errors": job.errors,
        "parts": [dict(p) for p in job.parts],
    }


@dataclass
class _TenantFetch:
    """State of fetching one log type of one tenant (one of the job's parts)."""

    db: Database
    job: FetchJob
    client: CpiClient
    tenant: dict
    log_type: str
    part: int
    files_total: int = 0
    files_done: int = 0
    imported: int = 0
    warnings: int = 0

    @property
    def tenant_id(self) -> str:
        return self.tenant["id"]

    def progress(self, file: str, new_rows: int) -> None:
        self.imported += new_rows
        part = self.job.update_part(self.part, files_done=self.files_done, imported=self.imported)
        self.job.push(
            {
                "type": "progress",
                "done": self.files_done,
                "total": self.files_total,
                "file": file,
                "new_rows": new_rows,
                "imported": self.job.imported,
                "part": part,
            }
        )

    def warn(self, msg: str, file: str) -> None:
        """A file could not be fetched or imported; the part goes on with the next file."""
        self.warnings += 1
        part = self.job.update_part(self.part, warnings=self.warnings)
        self.job.push({"type": "warn", "msg": msg, "file": file, "part": part})

    def fail(self, msg: str) -> None:
        """This tenant and log type cannot be fetched; the job goes on with the next part."""
        part = self.job.update_part(self.part, status="failed", error=msg)
        self.job.push({"type": "tenant_error", "msg": msg, "part": part})


async def _fetch_log_type(ctx: _TenantFetch, cutoff_ms: int) -> None:
    job, name = ctx.job, ctx.tenant["name"]
    job.current_tenant = name
    job.current_log_type = ctx.log_type
    job.status_msg = f"🔑 Requesting token for {name}…"
    job.push({"type": "status", "msg": job.status_msg, "part": job.update_part(ctx.part, status="running")})
    try:
        await ctx.client.get_token()
    except Exception as e:
        ctx.fail(f"Couldn't get an OAuth token for {name}: {e}")
        return
    try:
        files = await ctx.client.list_files(ctx.log_type)
    except Exception as e:
        ctx.fail(f"Couldn't list log files for {name}: {e}")
        return
    if cutoff_ms > 0:
        files = [f for f in files if f.last_modified_ms > cutoff_ms]

    ctx.files_total = job.total = len(files)
    job.status_msg = f"{name} · {ctx.log_type}: {len(files)} files"
    job.push(
        {
            "type": "files_found",
            "count": len(files),
            "tenant": name,
            "log_type": ctx.log_type,
            "part": job.update_part(ctx.part, files_total=len(files)),
        }
    )

    tenant_log_dir = get_settings().logs_dir / ctx.tenant_id
    tenant_log_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(get_settings().fetch_concurrency)

    async def bounded(f: RemoteLogFile) -> None:
        async with semaphore:
            if not job.cancel_requested:
                await _fetch_file(ctx, f, tenant_log_dir)

    await asyncio.gather(*[bounded(f) for f in files])
    status = "cancelled" if job.cancel_requested and ctx.files_done < ctx.files_total else "done"
    job.push({"type": "part", "part": job.update_part(ctx.part, status=status)})


async def _fetch_file(ctx: _TenantFetch, f: RemoteLogFile, tenant_log_dir: Path) -> None:
    """Download one file if it is new or has grown, and import the new rows."""
    job = ctx.job
    # The file name comes from the remote server; never let it point outside
    # this tenant's log directory.
    if not f.name or Path(f.name).name != f.name or f.name in (".", ".."):
        ctx.files_done += 1
        job.done = ctx.files_done
        ctx.warn(f"Skipped file with unsafe name: {f.name!r}", f.name)
        ctx.progress(f.name, 0)
        return
    dest = tenant_log_dir / f.name
    file_import = await file_imports_repo.get_file_import(ctx.db, ctx.tenant_id, ctx.log_type, f.name)

    # Backfill size from local file if missing (legacy imports)
    if file_import["lines"] > 0 and file_import["size"] == 0 and dest.exists():
        local_size = dest.stat().st_size
        await file_imports_repo.update_file_import_size(ctx.db, ctx.tenant_id, ctx.log_type, f.name, local_size)
        file_import["size"] = local_size

    ctx.files_done += 1
    job.done = ctx.files_done
    job.current_file = f.name
    job.status_msg = f"{ctx.tenant['name']} · {ctx.log_type}: {f.name} ({ctx.files_done}/{ctx.files_total})"

    # Skip if already fully imported (the size is recorded once a file was read to the end)
    if f.size > 0 and file_import["size"] == f.size:
        ctx.progress(f.name, 0)
        return

    # Download if new or grown
    if not dest.exists() or dest.stat().st_size != f.size:
        try:
            # Streamed to disk as gzip with constant memory; the parser
            # auto-detects gzip via magic bytes.
            await ctx.client.download(f, dest)
        except Exception as e:
            ctx.warn(f"Download failed for {f.name}: {e}", f.name)
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
        ctx.warn(f"Import failed: {f.name}: {e}", f.name)
        new_rows = 0
    job.imported += new_rows
    ctx.progress(f.name, new_rows)
