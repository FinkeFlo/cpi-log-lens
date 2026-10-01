"""Fetch job: token, file list, download, incremental import, progress, cancel and SSE,
against an in-process fake of the CPI API."""

import asyncio
import gzip
import json
import time

import pytest

from app.repositories import file_imports as file_imports_repo
from tests.support import FAKE_TENANT, app_db, fetch, numbered_lines, wait_for_job

pytestmark = pytest.mark.anyio

TRACE = {"tenants": ["fake"], "log_types": ["trace"], "hours": 0}


@pytest.fixture
async def tenant(client, fake_cpi):
    await client.post("/api/tenants", json=FAKE_TENANT)
    return fake_cpi


def downloads(fake):
    return [r for r in fake.requests if r.startswith("download")]


async def total(client, **params):
    return (await client.get("/api/logs", params=params)).json()["total"]


async def test_fetch_downloads_and_imports_every_file(client, tenant, app_env):
    tenant.add("a.log", numbered_lines(3))
    tenant.add("b.log", numbered_lines(2))
    tenant.add("h.log", numbered_lines(4), log_type="http")
    status = await fetch(client, **TRACE)
    assert status["status"] == "done"
    assert status["status_msg"] == "Completed"
    assert (status["done"], status["total"], status["imported"]) == (2, 2, 5)
    assert status["current_tenant"] == "Fake"
    assert status["current_log_type"] == "trace"
    assert tenant.requests[:2] == ["token", "list"]
    assert sorted(downloads(tenant)) == ["download a.log", "download b.log"]
    assert await total(client) == 5
    # Stored gzip-compressed per tenant, whatever the server sent.
    stored = app_env / "logs" / "fake" / "a.log"
    assert gzip.decompress(stored.read_bytes()).decode().count("\n") == 3


async def test_both_log_types(client, tenant):
    tenant.add("a.log", numbered_lines(3))
    tenant.add("h.log", numbered_lines(4), log_type="http")
    await fetch(client, tenants=["all"], log_types=["trace", "http"], hours=0)
    assert await total(client) == 7
    body = (await client.get("/api/stats")).json()
    assert sorted((r["log_type"], r["cnt"]) for r in body["per_tenant"]) == [("http", 4), ("trace", 3)]


async def test_unchanged_files_are_skipped_without_download(client, tenant):
    tenant.add("a.log", numbered_lines(3))
    await fetch(client, **TRACE)
    tenant.requests.clear()
    status = await fetch(client, **TRACE)
    assert status["imported"] == 0
    assert downloads(tenant) == []


async def test_grown_files_import_only_the_new_rows(client, tenant):
    tenant.add("a.log", numbered_lines(3))
    await fetch(client, **TRACE)
    tenant.add("a.log", numbered_lines(5))
    status = await fetch(client, **TRACE)
    assert status["imported"] == 2
    assert await total(client) == 5


async def test_hours_filters_files_by_last_modified(client, tenant):
    now_ms = int(time.time() * 1000)
    tenant.add("new.log", numbered_lines(1), last_modified_ms=now_ms - 3600_000)
    tenant.add("old.log", numbered_lines(1), last_modified_ms=now_ms - 3 * 3600_000)
    status = await fetch(client, tenants=["fake"], log_types=["trace"], hours=2)
    assert status["total"] == 1
    assert downloads(tenant) == ["download new.log"]


@pytest.mark.parametrize("name", ["../escape.log", "a/b.log", "..", ""])
async def test_unsafe_remote_file_names_are_skipped(client, tenant, app_env, name):
    tenant.add(name, numbered_lines(1))
    tenant.add("ok.log", numbered_lines(1))
    status = await fetch(client, **TRACE)
    assert status["imported"] == 1
    assert downloads(tenant) == ["download ok.log"]
    assert not (app_env / "logs" / "escape.log").exists()


async def test_transient_download_errors_are_retried(client, tenant):
    tenant.add("a.log", numbered_lines(2))
    tenant.download_status["a.log"] = [503, 500]
    status = await fetch(client, **TRACE)
    assert status["imported"] == 2
    assert downloads(tenant) == ["download a.log"] * 3


async def test_failed_download_is_skipped_and_retried_next_time(client, tenant):
    tenant.add("a.log", numbered_lines(2))
    tenant.download_status["a.log"] = [500, 500, 500]
    status = await fetch(client, **TRACE)
    assert status["status"] == "done"
    assert status["imported"] == 0
    status = await fetch(client, **TRACE)
    assert status["imported"] == 2


async def test_client_errors_are_not_retried(client, tenant):
    tenant.add("a.log", numbered_lines(2))
    tenant.download_status["a.log"] = [404]
    await fetch(client, **TRACE)
    assert downloads(tenant) == ["download a.log"]


async def test_token_error_ends_the_tenant_but_not_the_job(client, tenant):
    tenant.token_status = 401
    tenant.add("a.log", numbered_lines(2))
    status = await fetch(client, **TRACE)
    assert status["status"] == "done"
    assert status["imported"] == 0
    assert tenant.requests == ["token"]


async def test_list_errors_are_retried_then_reported(client, tenant):
    tenant.list_status = 500
    status = await fetch(client, **TRACE)
    assert status["status"] == "done"
    assert tenant.requests == ["token", "list", "list", "list"]


async def test_unknown_tenants_end_the_job_with_an_error(client, tenant):
    status = await fetch(client, tenants=["nope"], log_types=["trace"], hours=0)
    assert status["status"] == "error"
    assert status["error_msg"] == "No tenants configured."


async def test_legacy_import_without_size_is_backfilled_from_the_local_file(client, tenant, app_env):
    lines = numbered_lines(3)
    local = app_env / "logs" / "fake" / "a.log"
    local.parent.mkdir(parents=True)
    local.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    db = app_db()
    await db.run(
        db.execute,
        "INSERT INTO file_imports (tenant, log_type, filename, lines, size) VALUES ('fake', 'trace', 'a.log', 3, 0)",
    )
    tenant.add("a.log", lines, size=local.stat().st_size)
    status = await fetch(client, **TRACE)
    assert status["imported"] == 0
    assert downloads(tenant) == []
    expected = {"lines": 3, "size": local.stat().st_size}
    assert await file_imports_repo.get_file_import(db, "fake", "trace", "a.log") == expected


async def test_a_second_start_while_running_is_refused(client, tenant):
    tenant.add("a.log", numbered_lines(1))
    tenant.download_delay = 0.3
    first = (await client.post("/api/fetch", json=TRACE)).json()
    second = await client.post("/api/fetch", json=TRACE)
    assert second.status_code == 409
    assert second.json() == {"detail": "A fetch is already running.", "job_id": first["job_id"]}
    await wait_for_job(client)


async def test_cancel_stops_at_the_next_file(client, tenant, monkeypatch, settings):
    monkeypatch.setattr(settings, "fetch_concurrency", 1)
    for i in range(5):
        tenant.add(f"f{i}.log", numbered_lines(1))
    tenant.download_delay = 0.1
    await client.post("/api/fetch", json=TRACE)
    await asyncio.sleep(0.15)
    res = await client.post("/api/fetch/cancel")
    assert res.json()["ok"] is True
    status = await wait_for_job(client)
    assert status["status"] == "cancelled"
    assert status["status_msg"] == "Cancelled"
    assert 1 <= status["imported"] < 5
    assert await total(client) == status["imported"]


async def test_status_and_cancel_without_a_job(client):
    assert (await client.get("/api/fetch/status")).json() == {"status": "idle"}
    assert (await client.post("/api/fetch/cancel")).status_code == 409


def sse_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_stream_without_a_job_says_idle(client):
    res = await client.get("/api/fetch/stream")
    assert res.headers["content-type"].startswith("text/event-stream")
    assert sse_events(res.text) == [{"type": "idle"}]


async def test_stream_of_a_running_job_sends_snapshot_progress_and_done(client, tenant):
    tenant.add("a.log", numbered_lines(2))
    tenant.add("b.log", numbered_lines(3))
    tenant.download_delay = 0.2
    await client.post("/api/fetch", json=TRACE)
    events = sse_events((await client.get("/api/fetch/stream")).text)
    types = [e["type"] for e in events]
    assert types[0] == "snapshot"
    assert events[0]["status"] == "running"
    assert types[-1] == "done"
    assert events[-1]["imported"] == 5
    progress = [e for e in events if e["type"] == "progress"]
    assert {e["file"] for e in progress} <= {"a.log", "b.log"}
    assert all(set(e) == {"type", "done", "total", "file", "new_rows", "imported"} for e in progress)


async def test_stream_of_a_finished_job_sends_only_the_snapshot(client, tenant):
    tenant.add("a.log", numbered_lines(2))
    await fetch(client, **TRACE)
    events = sse_events((await client.get("/api/fetch/stream")).text)
    assert [e["type"] for e in events] == ["snapshot"]
    assert events[0]["status"] == "done"
    assert events[0]["imported"] == 2


# ── Demo data and MOCK mode ──────────────────────────────────────────────────


async def test_demo_creates_the_demo_tenant_and_imports_the_sample(client):
    res = await client.post("/api/demo")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    status = await wait_for_job(client)
    assert status["status"] == "done"
    assert status["imported"] == 1803
    tenants = (await client.get("/api/tenants")).json()
    assert [(t["id"], t["name"], t["api_url"]) for t in tenants] == [("demo", "Demo", "demo://sample")]
    # A second run finds nothing new.
    await client.post("/api/demo")
    assert (await wait_for_job(client))["imported"] == 0
    assert await total(client) == 1803


async def test_mock_mode_imports_the_sample_for_any_tenant(client, fake_cpi, monkeypatch, settings):
    monkeypatch.setattr(settings, "mock", True)
    await client.post("/api/tenants", json=FAKE_TENANT)
    status = await fetch(client, **TRACE)
    assert status["imported"] == 1803
    assert fake_cpi.requests == []


async def test_mock_mode_has_only_trace_samples(client, monkeypatch, settings):
    monkeypatch.setattr(settings, "mock", True)
    await client.post("/api/tenants", json=FAKE_TENANT)
    status = await fetch(client, tenants=["fake"], log_types=["trace", "http"], hours=0)
    assert status["imported"] == 1803
    assert status["current_log_type"] == "http"
    assert status["total"] == 0


# ── Saved default fetch form ─────────────────────────────────────────────────


async def test_default_fetch_config(client):
    assert (await client.get("/api/fetch/default-config")).json() is None
    cfg = {"tenants": ["all"], "log_types": ["http"], "hours": 6}
    assert (await client.put("/api/fetch/default-config", json=cfg)).json() == {"ok": True}
    assert (await client.get("/api/fetch/default-config")).json() == cfg
