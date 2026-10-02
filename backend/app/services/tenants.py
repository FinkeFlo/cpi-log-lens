"""Tenant seeding from TENANTS_CONFIG, the demo tenant, deleting tenants and the connection test."""

import asyncio
import logging
import shutil
from dataclasses import dataclass

import httpx
import json5

from app import cpi
from app.config import get_settings
from app.cpi import DEMO_URL
from app.cpi import errors as cpi_errors
from app.repositories import tenants as tenants_repo
from app.repositories.database import Database
from app.services import stats

log = logging.getLogger("cpi")

DEMO_TENANT_ID = "demo"


async def ensure_demo_tenant(db: Database) -> None:
    demo_url = f"{DEMO_URL}sample"
    await tenants_repo.upsert_tenant(db, DEMO_TENANT_ID, "Demo", demo_url, demo_url, "demo", "demo")


async def load_tenants_from_json(conn: Database) -> None:
    """Seed tenants from the optional TENANTS_CONFIG file (JSON with comments)."""
    settings = get_settings()
    config_path = settings.tenants_config
    if not config_path.is_file():
        if config_path.exists():
            log.warning("%s is not a file, ignoring it", config_path)
        return

    try:
        data = json5.loads(config_path.read_text())
        tenant_list = data.get("tenants", [])
    except Exception as e:
        log.warning(f"could not parse {config_path}: {e}")
        return

    added = updated = 0
    for t in tenant_list:
        tid = t.get("id", "").strip().lower()
        name = t.get("name", tid.upper())
        api = t.get("api_url", "")
        oauth = t.get("oauth_url", "")
        cid = t.get("client_id", "")
        secret = t.get("client_secret", "")
        if not (tid and api and cid):
            continue
        exists = await tenants_repo.get_tenant(conn, tid) is not None
        if exists and settings.tenants_seed_mode != "sync":
            continue
        await tenants_repo.upsert_tenant(conn, tid, name, api, oauth, cid, secret)
        updated += exists
        added += not exists
    log.info("tenants from %s: %d added, %d updated (mode %s)", config_path, added, updated, settings.tenants_seed_mode)


async def delete_tenant(db: Database, tenant_id: str, *, purge: bool = False) -> dict:
    """Delete a tenant and remove it from schedules. `purge` also deletes its log
    entries, import bookkeeping, unparsed lines and downloaded log files."""
    result = await tenants_repo.delete_tenant(db, tenant_id, purge=purge)
    if purge:
        stats.invalidate()
        logs_dir = get_settings().logs_dir.resolve()
        tenant_dir = logs_dir / tenant_id
        # Only a direct child of the log directory, whatever the id contains.
        if tenant_dir.resolve().parent == logs_dir and tenant_dir.name == tenant_id and tenant_dir.is_dir():
            await asyncio.to_thread(shutil.rmtree, tenant_dir)
    log.info(
        "tenant %s deleted (%s); %d entries deleted, %d schedules changed, %d deleted",
        tenant_id,
        "with its data" if purge else "data kept",
        result["deleted_entries"],
        result["schedules_changed"],
        result["schedules_deleted"],
    )
    return result


# The connection test makes one attempt per request with short timeouts, so a wrong
# host is reported within seconds instead of after the retries a fetch makes.
CONNECTION_TEST_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(frozen=True)
class ConnectionCheck:
    token: str
    trace_files: int  # trace log files the API lists


class ConnectionCheckFailed(Exception):
    """A connection test failed; `problem` says why in plain words."""

    def __init__(self, step: cpi_errors.Step, problem: cpi_errors.CpiProblem) -> None:
        super().__init__(problem.text())
        self.step = step
        self.problem = problem


async def check_connection(tenant: dict) -> ConnectionCheck:
    """Check connection details the way a fetch uses them: request an OAuth token
    (without following redirects), then list the trace log files, which needs a role
    that may read log files. Raises ConnectionCheckFailed with a readable reason; the
    original error goes to the log."""
    step: cpi_errors.Step = "token"
    async with cpi.client_for(tenant, timeout=CONNECTION_TEST_TIMEOUT, retries=1) as client:
        try:
            token = await client.get_token(follow_redirects=False)
            step = "api"
            files = await client.list_files("trace")
        except Exception as e:
            url = client.oauth_url if step == "token" else client.api_url
            log.info("connection test of %s failed at the %s request: %r", tenant.get("id") or "new tenant", step, e)
            raise ConnectionCheckFailed(step, cpi_errors.describe(e, step, url)) from e
    return ConnectionCheck(token=token, trace_files=len(files))
