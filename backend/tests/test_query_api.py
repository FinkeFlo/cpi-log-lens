"""Contract of the query API for scripts and LLM tools: POST /api/query and GET /api/query/schema.

External tools depend on these shapes; change them only on purpose."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.api.schemas import LLMQueryRequest
from app.repositories import tenants as tenants_repo
from app.services import importer
from tests.support import app_db, log_line, numbered_lines, write_log

pytestmark = pytest.mark.anyio

EXPECTED_SCHEMA: dict[str, Any] = {
    "description": (
        "CPI Log Lens query API. Use POST /api/query to search SAP CPI log entries. "
        "Use GET /api/stats to get aggregated statistics."
    ),
    "endpoints": {
        "POST /api/query": {
            "description": "Search log entries with optional filters.",
            "body": {
                "tenant": "string | null — tenant id to filter; null means all tenants",
                "level": "string | null — log level: ERROR, WARN, INFO, DEBUG",
                "iflow": "string | null — partial IFlow name (case-insensitive LIKE match)",
                "grep": (
                    "string | null — case-insensitive text search in message and logger; "
                    "when no range is supplied, searches the last 24 hours"
                ),
                "date_from": (
                    "string | null — inclusive ISO date/datetime lower bound; timezone-less values "
                    "are interpreted as UTC and offset-aware values are normalized to UTC"
                ),
                "date_to": (
                    "string | null — inclusive ISO date/datetime upper bound; a bare date includes the full day"
                ),
                "limit": "integer 1–200 — max entries to return (default 50)",
            },
            "response": {
                "total_matching": "total rows matching the filter (may exceed limit)",
                "returned": "number of rows returned",
                "summary": "short natural-language summary of the result",
                "items": "array of log entries",
                "item_fields": "id, tenant, log_type, filename, timestamp, level, logger, iflow, message, ip, node",
            },
        },
        "GET /api/stats": {
            "description": "Aggregated statistics (level distribution, top error iflows, timeline).",
            "params": {"tenant": "optional tenant id"},
        },
        "GET /api/tenants": {"description": "List configured tenants."},
    },
    "tip": "Start with GET /api/query/schema to understand the data, then POST /api/query with filters.",
}


@pytest.fixture
async def seeded(client, tmp_path):
    db = app_db()
    lines = [
        log_line(ts="2026-01-15 08:00:00", level="ERROR", thread="1-Demo_A_Worker-1", message="refused"),
        *numbered_lines(3, start_minute=60),
    ]
    path = write_log(tmp_path / "a.log", lines)
    await importer.import_log_file(db, "dev", "trace", path, "a.log", 0)
    await tenants_repo.upsert_tenant(db, "dev", "DEV", "https://x.example", "https://x.example/t", "c", "s")
    return client


async def test_schema_snapshot(seeded):
    body = (await seeded.get("/api/query/schema")).json()
    tenants = body.pop("available_tenants")
    assert tenants == [{"id": "dev", "name": "DEV"}]
    assert body == EXPECTED_SCHEMA


async def test_schema_body_matches_the_request_model():
    documented = set(EXPECTED_SCHEMA["endpoints"]["POST /api/query"]["body"])
    assert documented == set(LLMQueryRequest.model_fields)


async def test_response_shape_and_documented_item_fields(seeded):
    res = await seeded.post("/api/query", json={"limit": 2})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"total_matching", "returned", "summary", "items"}
    assert body["total_matching"] == 4
    assert body["returned"] == 2
    documented = EXPECTED_SCHEMA["endpoints"]["POST /api/query"]["response"]["item_fields"]
    # Every documented field is present; the list also carries imported_at.
    assert set(documented.split(", ")) <= set(body["items"][0])


async def test_summary_describes_filters_and_truncation(seeded):
    body = (
        await seeded.post(
            "/api/query",
            json={
                "tenant": "dev",
                "level": "error",
                "iflow": "Demo",
                "grep": "ref",
                "date_from": "2026-01-15",
                "date_to": "2026-01-15",
            },
        )
    ).json()
    assert body["summary"] == (
        "Found 1 log entry matching tenant=dev, level=ERROR, iflow~'Demo', grep='ref', "
        "range=[2026-01-15 → 2026-01-15]. Returning 1 of 1."
    )
    body = (await seeded.post("/api/query", json={"limit": 1})).json()
    assert body["summary"] == (
        "Found 4 log entries matching no filters. Returning 1 of 4. "
        "Use a stricter filter or increase limit (max 200) to see more."
    )


async def test_filters_behave_like_the_log_list(seeded):
    body = (await seeded.post("/api/query", json={"level": "ERROR", "date_to": "2026-01-15 08:00:00"})).json()
    assert [i["message"] for i in body["items"]] == ["refused"]
    body = (await seeded.post("/api/query", json={"date_to": "2026-01-15"})).json()
    assert body["total_matching"] == 4
    body = (
        await seeded.post(
            "/api/query",
            json={
                "iflow": "dEmO_a",
                "grep": "REFUSED",
                "date_from": "2026-01-15",
                "date_to": "2026-01-16",
            },
        )
    ).json()
    assert [item["message"] for item in body["items"]] == ["refused"]


async def test_offset_datetime_is_normalized_and_inverted_range_rejected(seeded):
    body = (
        await seeded.post(
            "/api/query",
            json={
                "date_from": "2026-01-15T09:00:00+01:00",
                "date_to": "2026-01-15T10:00:00+02:00",
            },
        )
    ).json()
    assert [item["message"] for item in body["items"]] == ["refused"]
    invalid = await seeded.post("/api/query", json={"date_from": "2026-01-16", "date_to": "2026-01-15"})
    assert invalid.status_code == 422
    assert "date_from" in invalid.text or "date_to" in invalid.text


async def test_text_search_defaults_to_a_bounded_window_and_reports_it(seeded, tmp_path):
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    stale = (now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    path = write_log(
        tmp_path / "text-window.log",
        [log_line(ts=stale, message="bounded needle"), log_line(ts=recent, message="bounded needle")],
    )
    await importer.import_log_file(app_db(), "dev", "trace", path, path.name, 0)

    body = (await seeded.post("/api/query", json={"grep": "needle"})).json()
    assert body["total_matching"] == 1
    assert [item["message"] for item in body["items"]] == ["bounded needle"]
    assert "range=[" in body["summary"]


@pytest.mark.parametrize(("limit", "returned"), [(0, 1), (-5, 1), (1000, 4)])
async def test_limit_is_clamped_to_1_to_200(seeded, limit, returned):
    body = (await seeded.post("/api/query", json={"limit": limit})).json()
    assert body["returned"] == returned


async def test_invalid_date_is_rejected_with_422(seeded):
    res = await seeded.post("/api/query", json={"date_from": "last week"})
    assert res.status_code == 422
    assert "date_from" in str(res.json()["detail"])


@pytest.mark.parametrize(
    ("endpoint", "params"),
    [
        ("/api/logs", {"date_from": "0001-01-01T00:00:00+01:00"}),
        ("/api/logs", {"date_to": "9999-12-31T23:59:59-01:00"}),
        ("/api/query", {"date_from": "0001-01-01T00:00:00+01:00"}),
        ("/api/query", {"date_to": "9999-12-31T23:59:59-01:00"}),
    ],
)
async def test_datetime_offset_outside_supported_range_is_rejected_with_422(seeded, endpoint, params):
    if endpoint == "/api/logs":
        res = await seeded.get(endpoint, params=params)
    else:
        res = await seeded.post(endpoint, json=params)
    assert res.status_code == 422


async def test_body_must_be_json(seeded):
    res = await seeded.post("/api/query", content="limit=5", headers={"Content-Type": "text/plain"})
    assert res.status_code == 422
