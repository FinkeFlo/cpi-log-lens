"""Contract of the query API for scripts and LLM tools: POST /api/query and GET /api/query/schema.

External tools depend on these shapes; change them only on purpose."""

from typing import Any

import pytest

from app import main
from app.repositories import database
from app.repositories import tenants as tenants_repo
from app.services import importer
from tests.support import log_line, numbered_lines, write_log

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
                "iflow": "string | null — partial iflow name (case-insensitive LIKE match)",
                "grep": "string | null — full-text search in message and logger fields",
                "date_from": "string | null — start datetime 'YYYY-MM-DD HH:MM:SS'",
                "date_to": "string | null — end datetime 'YYYY-MM-DD HH:MM:SS'",
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
    db = await database.get_db()
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
    assert documented == set(main.LLMQueryRequest.model_fields)


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
            json={"tenant": "dev", "level": "error", "iflow": "Demo", "grep": "ref", "date_from": "2026-01-15"},
        )
    ).json()
    assert body["summary"] == (
        "Found 1 log entry matching tenant=dev, level=ERROR, iflow~'Demo', grep='ref', "
        "range=[2026-01-15 → ...]. Returning 1 of 1."
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


@pytest.mark.parametrize(("limit", "returned"), [(0, 1), (-5, 1), (1000, 4)])
async def test_limit_is_clamped_to_1_to_200(seeded, limit, returned):
    body = (await seeded.post("/api/query", json={"limit": limit})).json()
    assert body["returned"] == returned


async def test_invalid_date_is_rejected_with_422(seeded):
    res = await seeded.post("/api/query", json={"date_from": "last week"})
    assert res.status_code == 422
    assert "date_from" in res.json()["detail"]


async def test_body_must_be_json(seeded):
    res = await seeded.post("/api/query", content="limit=5", headers={"Content-Type": "text/plain"})
    assert res.status_code == 422
