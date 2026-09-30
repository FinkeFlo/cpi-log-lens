"""Tenant seeding from TENANTS_CONFIG and the demo tenant."""

import logging

import json5

from app.config import get_settings
from app.repositories import tenants as tenants_repo
from app.repositories.database import Database

log = logging.getLogger("cpi")

# A tenant whose URLs use this scheme imports the bundled sample logs instead
# of calling SAP CPI, so new users can try the app without credentials.
DEMO_URL = "demo://"
DEMO_TENANT_ID = "demo"


def is_demo(tenant: dict) -> bool:
    return tenant["api_url"].startswith(DEMO_URL)


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
