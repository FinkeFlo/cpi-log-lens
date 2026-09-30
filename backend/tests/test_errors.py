"""Error responses that should use HTTP status codes. Several endpoints still answer
200 with {"ok": false}; the known cases are pinned here as expected failures."""

import pytest

from tests.support import FAKE_TENANT, numbered_lines, wait_for_job

pytestmark = pytest.mark.anyio

ARC_08 = "ARC-08: error answered with 200 and {'ok': false} instead of an HTTP status"


@pytest.mark.xfail(reason=ARC_08)
async def test_starting_a_second_fetch_is_409(client, fake_cpi):
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(1))
    fake_cpi.download_delay = 0.3
    body = {"tenants": ["fake"], "log_types": ["trace"], "hours": 0}
    await client.post("/api/fetch", json=body)
    try:
        assert (await client.post("/api/fetch", json=body)).status_code == 409
    finally:
        await wait_for_job(client)


@pytest.mark.xfail(reason=ARC_08)
async def test_cancel_without_a_running_fetch_is_409(client):
    assert (await client.post("/api/fetch/cancel")).status_code == 409


@pytest.mark.xfail(reason=ARC_08)
async def test_connection_test_of_an_unknown_tenant_is_404(client):
    assert (await client.post("/api/tenants/nope/test")).status_code == 404


@pytest.mark.xfail(reason=ARC_08)
async def test_failed_connection_test_is_502(client, fake_cpi):
    fake_cpi.token_status = 401
    await client.post("/api/tenants", json=FAKE_TENANT)
    assert (await client.post("/api/tenants/fake/test")).status_code == 502


@pytest.mark.xfail(reason="ARC-08: deleting an unknown resource answers 200")
@pytest.mark.parametrize("path", ["/api/tenants/nope", "/api/schedules/nope"])
async def test_deleting_an_unknown_resource_is_404(client, path):
    assert (await client.delete(path)).status_code == 404
