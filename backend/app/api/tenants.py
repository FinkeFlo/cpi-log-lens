"""Tenants (CPI connections)."""

from fastapi import APIRouter, HTTPException

from app import cpi
from app.api.deps import DbDep, SchedulesDep
from app.api.schemas import TenantCreate
from app.repositories import tenants as tenants_repo
from app.services import tenants as tenant_service

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


@router.get("")
async def list_tenants(db: DbDep):
    tenants = await tenants_repo.get_tenants(db)
    # Mask secrets in response
    for t in tenants:
        t["client_secret"] = "••••••••" if t.get("client_secret") else ""
    return tenants


@router.post("", status_code=201)
async def create_tenant(body: TenantCreate, db: DbDep):
    if not body.client_secret:
        raise HTTPException(422, "client_secret is required")
    await tenants_repo.upsert_tenant(
        db,
        body.id,
        body.name,
        body.api_url,
        body.oauth_url,
        body.client_id,
        body.client_secret,
    )
    return {"ok": True}


@router.put("/{tenant_id}")
async def update_tenant(tenant_id: str, body: TenantCreate, db: DbDep):
    existing = await tenants_repo.get_tenant(db, tenant_id)
    if not existing:
        raise HTTPException(404, "Tenant not found")
    # Keep existing secret if an empty or masked value is submitted —
    # the edit form never pre-fills the stored secret.
    secret = body.client_secret
    if not secret or set(secret) == {"•"}:
        secret = existing["client_secret"]
    await tenants_repo.upsert_tenant(
        db,
        tenant_id,
        body.name,
        body.api_url,
        body.oauth_url,
        body.client_id,
        secret,
    )
    return {"ok": True}


@router.delete("/{tenant_id}")
async def remove_tenant(tenant_id: str, db: DbDep, schedules: SchedulesDep, purge: bool = False):
    """Delete a tenant; schedules no longer include it. Its log entries, import
    bookkeeping and downloaded files are kept unless `purge=true`."""
    result = await tenant_service.delete_tenant(db, tenant_id, purge=purge)
    await schedules.reload()
    return {"ok": True, **result}


@router.post("/{tenant_id}/test")
async def test_tenant(tenant_id: str, db: DbDep):
    try:
        tenant = await tenants_repo.get_tenant(db, tenant_id)
        if not tenant:
            raise HTTPException(404, "Tenant not found")
        if cpi.is_demo(tenant):
            return {"ok": True, "demo": True}
        async with cpi.client_for(tenant, timeout=30) as client:
            token = await client.get_token(follow_redirects=False)
        return {"ok": bool(token), "token_preview": token[:12] + "…"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
