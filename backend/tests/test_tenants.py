"""Tenant management: CRUD, secret handling, connection test and seeding from TENANTS_CONFIG."""

import pytest

import db as database
import main
from config import get_settings
from tests.support import FAKE_TENANT, fetch, numbered_lines

pytestmark = pytest.mark.anyio

MASK = "••••••••"


async def stored_secret(tenant_id):
    tenant = await database.get_tenant(await database.get_db(), tenant_id)
    assert tenant is not None
    return tenant["client_secret"]


async def test_create_and_list_masks_the_secret(client):
    res = await client.post("/api/tenants", json=FAKE_TENANT)
    assert res.status_code == 201
    assert res.json() == {"ok": True}
    tenants = (await client.get("/api/tenants")).json()
    assert len(tenants) == 1
    t = tenants[0]
    assert {k: t[k] for k in FAKE_TENANT} == {**FAKE_TENANT, "client_secret": MASK}
    assert "created_at" in t
    assert await stored_secret("fake") == "secret"


async def test_tenants_are_listed_by_id(client):
    for tid in ("zeta", "alpha", "mid"):
        await client.post("/api/tenants", json={**FAKE_TENANT, "id": tid})
    assert [t["id"] for t in (await client.get("/api/tenants")).json()] == ["alpha", "mid", "zeta"]


async def test_creating_an_existing_id_overwrites_it(client):
    await client.post("/api/tenants", json=FAKE_TENANT)
    res = await client.post("/api/tenants", json={**FAKE_TENANT, "name": "Renamed", "client_secret": "new"})
    assert res.status_code == 201
    assert [t["name"] for t in (await client.get("/api/tenants")).json()] == ["Renamed"]
    assert await stored_secret("fake") == "new"


@pytest.mark.parametrize("secret", ["", MASK, "•••"])
async def test_update_with_empty_or_masked_secret_keeps_the_stored_one(client, secret):
    await client.post("/api/tenants", json=FAKE_TENANT)
    res = await client.put("/api/tenants/fake", json={**FAKE_TENANT, "name": "New name", "client_secret": secret})
    assert res.status_code == 200
    assert await stored_secret("fake") == "secret"
    assert (await client.get("/api/tenants")).json()[0]["name"] == "New name"


async def test_update_with_a_new_secret_replaces_it(client):
    await client.post("/api/tenants", json=FAKE_TENANT)
    await client.put("/api/tenants/fake", json={**FAKE_TENANT, "client_secret": "rotated"})
    assert await stored_secret("fake") == "rotated"


async def test_update_uses_the_id_from_the_path(client):
    await client.post("/api/tenants", json=FAKE_TENANT)
    await client.put("/api/tenants/fake", json={**FAKE_TENANT, "id": "other", "name": "X"})
    assert [(t["id"], t["name"]) for t in (await client.get("/api/tenants")).json()] == [("fake", "X")]


async def test_update_of_an_unknown_tenant_is_404(client):
    res = await client.put("/api/tenants/nope", json=FAKE_TENANT)
    assert res.status_code == 404
    assert res.json() == {"detail": "Tenant not found"}


async def test_delete_removes_the_tenant_and_keeps_its_logs(client, fake_cpi):
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(3))
    await fetch(client, tenants=["fake"], log_types=["trace"], hours=0)
    res = await client.delete("/api/tenants/fake")
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert (await client.get("/api/tenants")).json() == []
    assert (await client.get("/api/logs")).json()["total"] == 3


async def test_delete_of_an_unknown_tenant_answers_ok(client):
    # Current behaviour; see test_errors for the intended 404.
    res = await client.delete("/api/tenants/nope")
    assert res.status_code == 200


async def test_connection_test_gets_a_token(client, fake_cpi):
    await client.post("/api/tenants", json=FAKE_TENANT)
    res = await client.post("/api/tenants/fake/test")
    assert res.json() == {"ok": True, "token_preview": "fake-token…"}
    assert fake_cpi.requests == ["token"]


async def test_connection_test_reports_a_failed_token_request(client, fake_cpi):
    fake_cpi.token_status = 401
    await client.post("/api/tenants", json=FAKE_TENANT)
    res = await client.post("/api/tenants/fake/test")
    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert "401" in res.json()["error"]
    assert fake_cpi.requests == ["token"]  # 4xx is not retried


async def test_connection_test_of_the_demo_tenant_needs_no_request(client, fake_cpi):
    await database.upsert_tenant(await database.get_db(), "demo", "Demo", "demo://sample", "demo://sample", "d", "d")
    assert (await client.post("/api/tenants/demo/test")).json() == {"ok": True, "demo": True}
    assert fake_cpi.requests == []


# ── Seeding from TENANTS_CONFIG ──────────────────────────────────────────────

SEED = """
// comments are allowed
{
  "tenants": [
    { "id": " DEV ", "name": "Development", "api_url": "https://dev.example", "oauth_url": "https://dev.example/t",
      "client_id": "c1", "client_secret": "s1" },
    { "id": "noapi", "client_id": "c2" },   // skipped: no api_url
    { "id": "qa", "api_url": "https://qa.example", "client_id": "c3", },  // trailing commas too
  ],
}
"""


async def seed(monkeypatch, tmp_path, text, mode="create"):
    path = tmp_path / "tenants.jsonc"
    path.write_text(text)
    settings = get_settings()
    monkeypatch.setattr(settings, "tenants_config", path)
    monkeypatch.setattr(settings, "tenants_seed_mode", mode)
    await main._load_tenants_from_json()
    return await database.get_tenants(await database.get_db())


async def test_seed_adds_tenants_and_normalizes_ids(db, monkeypatch, tmp_path):
    tenants = await seed(monkeypatch, tmp_path, SEED)
    assert [(t["id"], t["name"], t["client_secret"]) for t in tenants] == [
        ("dev", "Development", "s1"),
        ("qa", "QA", ""),
    ]


async def test_seed_in_create_mode_keeps_existing_tenants(db, monkeypatch, tmp_path):
    await database.upsert_tenant(db, "dev", "Edited in UI", "https://ui.example", "https://ui.example/t", "c", "s")
    tenants = await seed(monkeypatch, tmp_path, SEED)
    assert tenants[0]["name"] == "Edited in UI"


async def test_seed_in_sync_mode_overwrites_existing_tenants(db, monkeypatch, tmp_path):
    await database.upsert_tenant(db, "dev", "Edited in UI", "https://ui.example", "https://ui.example/t", "c", "s")
    tenants = await seed(monkeypatch, tmp_path, SEED, mode="sync")
    assert tenants[0]["name"] == "Development"


async def test_invalid_seed_file_is_ignored(db, monkeypatch, tmp_path):
    assert await seed(monkeypatch, tmp_path, "{ not json") == []


async def test_missing_or_directory_seed_path_is_ignored(db, monkeypatch, tmp_path, settings):
    monkeypatch.setattr(settings, "tenants_config", tmp_path)
    await main._load_tenants_from_json()
    monkeypatch.setattr(settings, "tenants_config", tmp_path / "missing.jsonc")
    await main._load_tenants_from_json()
    assert await database.get_tenants(db) == []
