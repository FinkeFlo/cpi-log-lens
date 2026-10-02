"""Fetch job: token, file list, download, incremental import, progress, cancel and SSE,
against an in-process fake of the CPI API."""

import asyncio
import gzip
import json
import time

import pytest

from app.repositories import file_imports as file_imports_repo
from app.services import fetch as fetch_service
from app.services.tenants import ensure_demo_tenant
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
    assert status["parts"][0]["error"] == (
        "Couldn't list log files for Fake. The CPI API had an internal error (HTTP 500). Try again later."
    )


async def test_status_reports_progress_per_tenant_and_log_type(client, tenant):
    tenant.add("a.log", numbered_lines(3))
    tenant.add("b.log", numbered_lines(2, start_minute=10))
    tenant.add("h.log", numbered_lines(4), log_type="http")
    status = await fetch(client, tenants=["fake"], log_types=["trace", "http"], hours=0)
    assert status["status_msg"] == "Completed"
    assert (status["files_total"], status["files_done"], status["warnings"], status["errors"]) == (3, 3, 0, 0)
    assert status["problems"] == []
    assert status["parts"] == [
        {
            "index": 0,
            "tenant": "fake",
            "tenant_name": "Fake",
            "log_type": "trace",
            "status": "done",
            "files_total": 2,
            "files_done": 2,
            "imported": 5,
            "warnings": 0,
            "error": "",
        },
        {
            "index": 1,
            "tenant": "fake",
            "tenant_name": "Fake",
            "log_type": "http",
            "status": "done",
            "files_total": 1,
            "files_done": 1,
            "imported": 4,
            "warnings": 0,
            "error": "",
        },
    ]


async def test_a_failing_tenant_is_reported_and_the_others_are_fetched(client, tenant):
    await ensure_demo_tenant(app_db())
    tenant.token_status = 401
    status = await fetch(client, tenants=["fake", "demo"], log_types=["trace"], hours=0)
    assert status["status"] == "done"
    assert status["status_msg"] == "Completed with errors"
    assert status["error_msg"] == ""
    assert status["imported"] == 1803
    assert [(p["tenant"], p["status"]) for p in status["parts"]] == [("fake", "failed"), ("demo", "done")]
    assert status["parts"][0]["error"] == (
        "Couldn't get an OAuth token for Fake. The OAuth server rejected the client ID or secret (HTTP 401). "
        "Copy the client ID and secret from the service key again."
    )
    assert status["parts"][1]["imported"] == 1803
    assert status["errors"] == 1
    [problem] = status["problems"]
    assert (problem["level"], problem["tenant"], problem["tenant_name"], problem["log_type"]) == (
        "error",
        "fake",
        "Fake",
        "trace",
    )
    assert problem["msg"] == status["parts"][0]["error"]


async def test_file_warnings_are_reported_with_their_part(client, tenant):
    tenant.add("a.log", numbered_lines(2))
    tenant.add("b.log", numbered_lines(2, start_minute=10))
    tenant.download_status["b.log"] = [404]
    status = await fetch(client, **TRACE)
    assert status["status_msg"] == "Completed with warnings"
    assert (status["warnings"], status["errors"]) == (1, 0)
    [part] = status["parts"]
    assert (part["status"], part["files_done"], part["imported"], part["warnings"]) == ("done", 2, 2, 1)
    [problem] = status["problems"]
    assert (problem["level"], problem["tenant"], problem["file"]) == ("warn", "fake", "b.log")
    assert problem["msg"].startswith("Download failed for b.log")


async def test_only_the_latest_problems_are_kept(client, tenant, monkeypatch, settings):
    monkeypatch.setattr(fetch_service, "MAX_PROBLEMS", 2)
    monkeypatch.setattr(settings, "fetch_concurrency", 1)  # files in listing order
    for i in range(4):
        tenant.add(f"f{i}.log", numbered_lines(1))
        tenant.download_status[f"f{i}.log"] = [404]
    status = await fetch(client, **TRACE)
    assert status["warnings"] == 4
    assert [p["file"] for p in status["problems"]] == ["f2.log", "f3.log"]


async def test_an_unexpected_error_ends_every_part(client, tenant, monkeypatch):
    tenant.add("a.log", numbered_lines(2))

    async def broken(*args, **kwargs):
        raise RuntimeError("database file is broken")

    monkeypatch.setattr(file_imports_repo, "get_file_import", broken)
    status = await fetch(client, tenants=["fake"], log_types=["trace", "http"], hours=0)
    assert status["status"] == "error"
    assert status["error_msg"] == "database file is broken"
    assert [(p["status"], p["error"]) for p in status["parts"]] == [
        ("failed", "database file is broken"),
        ("failed", "Not fetched: the job stopped with an error."),
    ]


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
    await client.post("/api/fetch", json={**TRACE, "log_types": ["trace", "http"]})
    await asyncio.sleep(0.15)
    res = await client.post("/api/fetch/cancel")
    assert res.json()["ok"] is True
    status = await wait_for_job(client)
    assert status["status"] == "cancelled"
    assert status["status_msg"] == "Cancelled"
    assert 1 <= status["imported"] < 5
    assert await total(client) == status["imported"]
    assert [p["status"] for p in status["parts"]] == ["cancelled", "cancelled"]
    assert status["parts"][0]["files_done"] < 5
    assert status["parts"][1]["files_total"] is None


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
    assert all(set(e) == {"type", "done", "total", "file", "new_rows", "imported", "part"} for e in progress)
    assert all((e["part"]["tenant"], e["part"]["log_type"]) == ("fake", "trace") for e in progress)
    assert {k: v for k, v in events[-1].items() if k != "parts"} == {
        "type": "done",
        "imported": 5,
        "warnings": 0,
        "errors": 0,
    }
    assert [p["status"] for p in events[-1]["parts"]] == ["done"]


async def test_stream_goes_on_after_a_tenant_error(db):
    release = asyncio.Event()

    async def runner(db, job, params):
        await release.wait()
        job.push({"type": "tenant_error", "msg": "Couldn't get an OAuth token for A: 401"})
        job.push({"type": "progress", "done": 1, "total": 1, "file": "b.log", "new_rows": 2, "imported": 2})
        await job.finish("done", {"type": "done", "imported": 2})

    service = fetch_service.FetchService(db, runner=runner)
    await service.start(fetch_service.FetchParams(["all"], ["trace"], 0))
    stream = service.event_stream()
    chunks = [await anext(stream)]
    release.set()
    chunks += [chunk async for chunk in stream if chunk.startswith("data: ")]
    assert [e["type"] for e in sse_events("".join(chunks))] == ["snapshot", "tenant_error", "progress", "done"]


async def test_stream_misses_no_event_sent_right_after_its_snapshot(db):
    release = asyncio.Event()

    async def runner(db, job, params):
        await release.wait()
        await job.finish("done", {"type": "done", "imported": 0})

    service = fetch_service.FetchService(db, runner=runner)
    job = await service.start(fetch_service.FetchParams(["all"], ["trace"], 0))
    stream = service.event_stream()
    chunks = [await anext(stream)]  # the snapshot; the client has not asked for more yet
    job.set_parts([("a", "A", "trace")])
    release.set()
    chunks += [chunk async for chunk in stream if chunk.startswith("data: ")]
    assert [e["type"] for e in sse_events("".join(chunks))] == ["snapshot", "parts", "done"]


async def test_stream_closes_after_a_cancel(db):
    release = asyncio.Event()

    async def runner(db, job, params):
        await release.wait()
        await job.finish("cancelled", {"type": "cancelled", "imported": 0})

    service = fetch_service.FetchService(db, runner=runner)
    await service.start(fetch_service.FetchParams(["all"], ["trace"], 0))
    stream = service.event_stream()
    chunks = [await anext(stream)]
    release.set()
    chunks += [chunk async for chunk in stream if chunk.startswith("data: ")]
    assert [e["type"] for e in sse_events("".join(chunks))] == ["snapshot", "cancelled"]


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
