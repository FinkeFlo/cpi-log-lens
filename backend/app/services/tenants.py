"""Tenant seeding from TENANTS_CONFIG and the demo tenant."""

import asyncio
import logging
import shutil

import json5

from app.config import get_settings
from app.cpi import DEMO_URL
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
