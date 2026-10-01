"""Tenant management: CRUD, secret handling, connection test and seeding from TENANTS_CONFIG."""

import pytest

from app.config import get_settings
from app.repositories import tenants as tenants_repo
from app.services import tenants as tenant_service
from tests.support import FAKE_TENANT, app_db, fetch, numbered_lines

pytestmark = pytest.mark.anyio

MASK = "••••••••"


async def stored_secret(tenant_id):
    tenant = await tenants_repo.get_tenant(app_db(), tenant_id)
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


async def test_delete_removes_the_tenant_and_keeps_its_logs(client, fake_cpi, settings):
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(3))
    await fetch(client, tenants=["fake"], log_types=["trace"], hours=0)
    res = await client.delete("/api/tenants/fake")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "deleted_entries": 0, "schedules_changed": 0, "schedules_deleted": 0}
    assert (await client.get("/api/tenants")).json() == []
    assert (await client.get("/api/logs")).json()["total"] == 3
    assert (settings.logs_dir / "fake" / "a.log").exists()
    # Created again, the tenant continues where it stopped: nothing is imported twice.
    await client.post("/api/tenants", json=FAKE_TENANT)
    fake_cpi.add("a.log", numbered_lines(4))
    assert (await fetch(client, tenants=["fake"], log_types=["trace"], hours=0))["imported"] == 1
    assert (await client.get("/api/logs")).json()["total"] == 4


async def test_delete_with_purge_removes_all_data_of_the_tenant(client, fake_cpi, settings):
    await client.post("/api/tenants", json=FAKE_TENANT)
    await client.post("/api/tenants", json={**FAKE_TENANT, "id": "other"})
    fake_cpi.add("a.log", ["not a log line", *numbered_lines(3)])
    await fetch(client, tenants=["all"], log_types=["trace"], hours=0)
    assert (await client.get("/api/logs")).json()["total"] == 6
    res = await client.delete("/api/tenants/fake", params={"purge": "true"})
    assert res.json()["deleted_entries"] == 3
    db = app_db()
    for table in ("logs", "file_imports", "unparsed_lines"):
        n = await db.read(db.fetch_val, f"SELECT count(*) FROM {table} WHERE tenant = 'fake'")
        assert n == 0, table
        n = await db.read(db.fetch_val, f"SELECT count(*) FROM {table} WHERE tenant = 'other'")
        assert n > 0, table
    assert not (settings.logs_dir / "fake").exists()
    assert (settings.logs_dir / "other").exists()
    assert (await client.get("/api/stats")).json()["total"] == 3
    # Created again, the tenant imports its files from scratch.
    await client.post("/api/tenants", json=FAKE_TENANT)
    assert (await fetch(client, tenants=["fake"], log_types=["trace"], hours=0))["imported"] == 3


async def test_purge_only_removes_the_tenant_directory(client, settings):
    (settings.logs_dir / "x").mkdir(parents=True)
    (settings.logs_dir / "keep").mkdir()
    outside = settings.logs_dir.parent / "outside"
    outside.mkdir()
    for tenant_id in ("x", "../outside", "keep/..", "."):
        await tenant_service.delete_tenant(app_db(), tenant_id, purge=True)
    assert not (settings.logs_dir / "x").exists()
    assert (settings.logs_dir / "keep").exists()
    assert outside.exists()


async def test_delete_removes_the_tenant_from_schedules(client):
    await client.post("/api/tenants", json=FAKE_TENANT)
    base = {"log_types": ["trace"], "hours": 1, "interval_minutes": 15}
    await client.post("/api/schedules", json={**base, "name": "only fake", "tenants": ["fake"]})
    await client.post("/api/schedules", json={**base, "name": "fake and qa", "tenants": ["fake", "qa"]})
    await client.post("/api/schedules", json={**base, "name": "all", "tenants": ["all"]})
    res = await client.delete("/api/tenants/fake")
    assert res.json()["schedules_changed"] == 1
    assert res.json()["schedules_deleted"] == 1
    schedules = {s["name"]: s["tenants"] for s in (await client.get("/api/schedules")).json()}
    assert schedules == {"fake and qa": ["qa"], "all": ["all"]}


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
    await tenants_repo.upsert_tenant(app_db(), "demo", "Demo", "demo://sample", "demo://sample", "d", "d")
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


async def seed(db, monkeypatch, tmp_path, text, mode="create"):
    path = tmp_path / "tenants.jsonc"
    path.write_text(text)
    settings = get_settings()
    monkeypatch.setattr(settings, "tenants_config", path)
    monkeypatch.setattr(settings, "tenants_seed_mode", mode)
    await tenant_service.load_tenants_from_json(db)
    return await tenants_repo.get_tenants(db)


async def test_seed_adds_tenants_and_normalizes_ids(db, monkeypatch, tmp_path):
    tenants = await seed(db, monkeypatch, tmp_path, SEED)
    assert [(t["id"], t["name"], t["client_secret"]) for t in tenants] == [
        ("dev", "Development", "s1"),
        ("qa", "QA", ""),
    ]


async def test_seed_in_create_mode_keeps_existing_tenants(db, monkeypatch, tmp_path):
    await tenants_repo.upsert_tenant(db, "dev", "Edited in UI", "https://ui.example", "https://ui.example/t", "c", "s")
    tenants = await seed(db, monkeypatch, tmp_path, SEED)
    assert tenants[0]["name"] == "Edited in UI"


async def test_seed_in_sync_mode_overwrites_existing_tenants(db, monkeypatch, tmp_path):
    await tenants_repo.upsert_tenant(db, "dev", "Edited in UI", "https://ui.example", "https://ui.example/t", "c", "s")
    tenants = await seed(db, monkeypatch, tmp_path, SEED, mode="sync")
    assert tenants[0]["name"] == "Development"


async def test_invalid_seed_file_is_ignored(db, monkeypatch, tmp_path):
    assert await seed(db, monkeypatch, tmp_path, "{ not json") == []


async def test_missing_or_directory_seed_path_is_ignored(db, monkeypatch, tmp_path, settings):
    monkeypatch.setattr(settings, "tenants_config", tmp_path)
    await tenant_service.load_tenants_from_json(db)
    monkeypatch.setattr(settings, "tenants_config", tmp_path / "missing.jsonc")
    await tenant_service.load_tenants_from_json(db)
    assert await tenants_repo.get_tenants(db) == []
