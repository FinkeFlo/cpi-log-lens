"""Tenants (CPI connections)."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app import cpi
from app.api.deps import DbDep, SchedulesDep
from app.api.schemas import ConnectionTestRequest, TenantCreate
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
    bookkeeping and downloaded files are kept unless `purge=true`. With purge, the
    data of a tenant that was deleted earlier can be removed as well."""
    if not purge and await tenants_repo.get_tenant(db, tenant_id) is None:
        raise HTTPException(404, "Tenant not found")
    result = await tenant_service.delete_tenant(db, tenant_id, purge=purge)
    await schedules.reload()
    return {"ok": True, **result}


async def _check(tenant: dict) -> tenant_service.ConnectionCheck | JSONResponse:
    """The connection check, or its failure as a 502 answer: `detail` (message and hint),
    `kind`, `step` (token or api), `message`, `hint` and the CPI side's `upstream_status`."""
    try:
        return await tenant_service.check_connection(tenant)
    except tenant_service.ConnectionCheckFailed as e:
        p = e.problem
        return JSONResponse(
            {
                "detail": p.text(),
                "kind": p.kind,
                "step": e.step,
                "message": p.message,
                "hint": p.hint,
                "upstream_status": p.status,
            },
            status_code=502,
        )


@router.post("/test")
async def test_connection_details(body: ConnectionTestRequest, db: DbDep):
    """Test connection details before saving them, like the test of a saved tenant.
    Without a client secret (empty or masked), the stored secret of tenant `id` is
    used, but only with that tenant's saved URLs."""
    tenant = body.model_dump()
    if not body.client_secret or set(body.client_secret) == {"•"}:
        stored = await tenants_repo.get_tenant(db, body.id) if body.id else None
        if stored is None:
            raise HTTPException(422, "Enter the client secret.")
        if (stored["api_url"], stored["oauth_url"]) != (body.api_url, body.oauth_url):
            raise HTTPException(
                422, "Enter the client secret to test changed URLs: the saved secret is only sent to the saved URLs."
            )
        tenant["client_secret"] = stored["client_secret"]
    result = await _check(tenant)
    if isinstance(result, JSONResponse):
        return result
    return {"ok": True, "trace_files": result.trace_files}


@router.post("/{tenant_id}/test")
async def test_tenant(tenant_id: str, db: DbDep):
    """Test a saved tenant's connection: request an OAuth token, then list the trace
    log files (which needs a role that may read log files). 200 with the number of
    trace log files when both work, 502 with a readable reason when not."""
    tenant = await tenants_repo.get_tenant(db, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if cpi.is_demo(tenant):
        return {"ok": True, "demo": True}
    result = await _check(tenant)
    if isinstance(result, JSONResponse):
        return result
    return {"ok": True, "trace_files": result.trace_files, "token_preview": result.token[:12] + "…"}
