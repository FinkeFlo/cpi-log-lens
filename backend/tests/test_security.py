"""Network safety defaults: trusted hosts, cross-origin write guard, no CORS by default."""

import pytest

from tests.support import FAKE_TENANT

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "192.0.2.1"])
async def test_unknown_host_names_are_rejected(client, host):
    res = await client.get("/api/tenants", headers={"Host": host})
    assert res.status_code == 400
    assert res.text == "Invalid host header"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "localhost:8080", "127.0.0.1:18200"])
async def test_allowed_host_names_are_served(client, host):
    assert (await client.get("/api/tenants", headers={"Host": host})).status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/tenants", FAKE_TENANT),
        ("PUT", "/api/tenants/fake", FAKE_TENANT),
        ("DELETE", "/api/tenants/fake", None),
        ("POST", "/api/db/clear", None),
        ("POST", "/api/fetch", {}),
    ],
)
async def test_writes_from_another_origin_are_refused(client, method, path, body):
    res = await client.request(method, path, json=body, headers={"Origin": "http://evil.example"})
    assert res.status_code == 403
    assert res.json() == {"detail": "cross-origin request refused"}


async def test_refused_write_has_no_effect(client):
    await client.post("/api/tenants", json=FAKE_TENANT, headers={"Origin": "http://localhost:3000"})
    assert (await client.get("/api/tenants")).json() == []


@pytest.mark.parametrize(
    "headers",
    [
        {},  # scripts and tools send no Origin
        {"Origin": "http://localhost"},  # the app's own origin
    ],
)
async def test_same_origin_and_originless_writes_are_allowed(client, headers):
    assert (await client.post("/api/tenants", json=FAKE_TENANT, headers=headers)).status_code == 201


async def test_reads_from_another_origin_are_answered_without_cors_headers(client):
    res = await client.get("/api/tenants", headers={"Origin": "http://evil.example"})
    assert res.status_code == 200
    assert "access-control-allow-origin" not in res.headers


async def test_configured_cors_origin_may_write(client, monkeypatch, settings):
    monkeypatch.setattr(settings, "cors_origins", ["https://dashboard.example"])
    res = await client.post("/api/tenants", json=FAKE_TENANT, headers={"Origin": "https://dashboard.example"})
    assert res.status_code == 201


@pytest.mark.parametrize("path", ["/js/main.js", "/js/pages/browse.js", "/vendor/alpine.esm.min.js"])
async def test_scripts_are_served_as_uncached_modules(client, path):
    res = await client.get(path)
    assert res.status_code == 200
    # Browsers refuse to run a module script without a JavaScript content type.
    assert res.headers["content-type"].split(";")[0] in ("text/javascript", "application/javascript")
    assert res.headers["cache-control"] == "no-cache, no-store, must-revalidate"


async def test_frontend_is_served(client):
    res = await client.get("/")
    assert res.status_code == 200
    assert "<title>CPI Log Lens</title>" in res.text
