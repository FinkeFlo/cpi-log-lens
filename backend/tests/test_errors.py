"""Error contract: every error is an HTTP status code with a JSON body {"detail": ...}."""

import httpx
import pytest

from app import main
from app.repositories import tenants as tenants_repo
from tests.support import FAKE_TENANT, numbered_lines, wait_for_job

pytestmark = pytest.mark.anyio


async def test_starting_a_second_fetch_is_409(client, fake_cpi):
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(1))
    fake_cpi.download_delay = 0.3
    body = {"tenants": ["fake"], "log_types": ["trace"], "hours": 0}
    first = (await client.post("/api/fetch", json=body)).json()
    try:
        for path, json_body in (("/api/fetch", body), ("/api/demo", None)):
            res = await client.post(path, json=json_body)
            assert res.status_code == 409
            assert res.json() == {"detail": "A fetch is already running.", "job_id": first["job_id"]}
    finally:
        await wait_for_job(client)


async def test_cancel_without_a_running_fetch_is_409(client):
    res = await client.post("/api/fetch/cancel")
    assert res.status_code == 409
    assert res.json() == {"detail": "No fetch is running."}


async def test_connection_test_of_an_unknown_tenant_is_404(client):
    res = await client.post("/api/tenants/nope/test")
    assert res.status_code == 404
    assert res.json() == {"detail": "Tenant not found"}


async def test_failed_connection_test_is_502(client, fake_cpi):
    fake_cpi.token_status = 401
    await client.post("/api/tenants", json=FAKE_TENANT)
    res = await client.post("/api/tenants/fake/test")
    assert res.status_code == 502
    body = res.json()
    assert body["detail"].startswith("The OAuth server rejected the client ID or secret (HTTP 401).")
    assert (body["kind"], body["step"], body["upstream_status"]) == ("invalid_credentials", "token", 401)


@pytest.mark.parametrize(
    ("method", "path", "detail"),
    [
        ("DELETE", "/api/tenants/nope", "Tenant not found"),
        ("DELETE", "/api/schedules/nope", "Schedule not found"),
        ("GET", "/api/logs/123", "Entry not found"),
        ("GET", "/api/fetch/runs/nope", "Fetch run not found"),
    ],
)
async def test_unknown_resources_are_404(client, method, path, detail):
    res = await client.request(method, path)
    assert res.status_code == 404
    assert res.json() == {"detail": detail}


async def test_validation_errors_are_422_with_the_invalid_fields(client):
    res = await client.post("/api/fetch", json={"hours": -1})
    assert res.status_code == 422
    [error] = res.json()["detail"]
    assert error["loc"] == ["body", "hours"]


async def test_unexpected_errors_are_500_json(client, monkeypatch):
    async def broken(db):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(tenants_repo, "get_tenants", broken)
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
        res = await c.get("/api/tenants")
    assert res.status_code == 500
    assert res.json() == {"detail": "Internal server error"}
