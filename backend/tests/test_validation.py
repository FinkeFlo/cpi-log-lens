"""Request validation: out-of-range or malformed input is rejected with 422 before it reaches
the database or the CPI API."""

import pytest

pytestmark = pytest.mark.anyio

TENANT = {
    "id": "dev",
    "name": "DEV",
    "api_url": "https://cpi.example.com",
    "oauth_url": "https://auth.example.com/oauth/token",
    "client_id": "client",
    "client_secret": "secret",
}
SCHEDULE = {"name": "every 15 min", "tenants": ["all"], "log_types": ["trace"], "hours": 1, "interval_minutes": 15}


@pytest.mark.parametrize(
    "change",
    [
        {"id": ""},
        {"id": "all"},
        {"id": "Dev"},
        {"id": "../x"},
        {"id": "with space"},
        {"id": "-dev"},
        {"id": "a" * 33},
        {"name": ""},
        {"api_url": "ftp://cpi.example.com"},
        {"api_url": "cpi.example.com"},
        {"oauth_url": "https://auth example.com"},
        {"client_id": ""},
        {"client_secret": "x" * 2001},
    ],
)
async def test_invalid_tenants_are_rejected(client, change):
    res = await client.post("/api/tenants", json={**TENANT, **change})
    assert res.status_code == 422, res.text


@pytest.mark.parametrize("tenant_id", ["d", "dev", "prd-eu_10", "a" * 32, "0x"])
async def test_valid_tenant_ids(client, tenant_id):
    res = await client.post("/api/tenants", json={**TENANT, "id": tenant_id})
    assert res.status_code == 201, res.text


async def test_new_tenant_requires_a_secret(client):
    res = await client.post("/api/tenants", json={**TENANT, "client_secret": ""})
    assert res.status_code == 422
    assert res.json()["detail"] == "client_secret is required"


@pytest.mark.parametrize(
    "body",
    [
        {"hours": -1},
        {"hours": 24 * 365 + 1},
        {"log_types": []},
        {"log_types": ["trace", "audit"]},
        {"tenants": []},
        {"hours": "many"},
    ],
)
async def test_invalid_fetch_requests_are_rejected(client, body):
    res = await client.post("/api/fetch", json=body)
    assert res.status_code == 422, res.text
    assert (await client.get("/api/fetch/status")).json() == {"status": "idle"}


@pytest.mark.parametrize(
    "change",
    [
        {"interval_minutes": 4},
        {"interval_minutes": 0},
        {"interval_minutes": 7 * 24 * 60 + 1},
        {"hours": -1},
        {"name": ""},
        {"log_types": ["x"]},
        {"tenants": []},
    ],
)
async def test_invalid_schedules_are_rejected(client, change):
    res = await client.post("/api/schedules", json={**SCHEDULE, **change})
    assert res.status_code == 422, res.text


@pytest.mark.parametrize("body", [{"hours": -1}, {"log_types": []}, {"tenants": []}])
async def test_invalid_default_fetch_config_is_rejected(client, body):
    assert (await client.put("/api/fetch/default-config", json=body)).status_code == 422


@pytest.mark.parametrize("days", [0, -1, 36501, "x"])
async def test_invalid_cleanup_age_is_rejected(client, days):
    assert (await client.post("/api/db/cleanup", json={"older_than_days": days})).status_code == 422


async def test_log_entry_id_must_be_an_integer(client):
    assert (await client.get("/api/logs/abc")).status_code == 422
