"""Log list filters and paging (GET /api/logs)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.services import importer
from tests.support import app_db, log_line, numbered_lines, write_log

pytestmark = pytest.mark.anyio

ROWS = {
    "a": [
        log_line(ts="2026-01-15 08:00:00", level="ERROR", thread="1-Demo_A_Worker-1", message="connection refused"),
        log_line(ts="2026-01-15 12:00:00", level="INFO", thread="1-Demo_B_Worker-1", message="all good"),
        log_line(ts="2026-01-16 00:00:00", level="WARN", thread="1-Demo_A_Worker-1", message="slow 50% done"),
    ],
    "b": [
        log_line(ts="2026-01-15 23:59:59", level="error", thread="1-Demo_C_Worker-1", message="refused again"),
        log_line(
            ts="2026-01-17 10:30:00",
            level="DEBUG",
            logger="com.example.Special",
            thread="1-Demo_C_Worker-1",
            message="x",
        ),
    ],
}


@pytest.fixture
async def seeded(client, tmp_path):
    db = app_db()
    for tenant, lines in ROWS.items():
        path = write_log(tmp_path / f"{tenant}.log", lines)
        await importer.import_log_file(db, tenant, "trace", path, path.name, 0)
    return client


async def messages(client, **params):
    if "grep" in params and "date_from" not in params and "date_to" not in params:
        params.update(date_from="2026-01-15", date_to="2026-01-18")
    res = await client.get("/api/logs", params=params)
    assert res.status_code == 200, res.text
    return [item["message"] for item in res.json()["items"]]


async def test_list_is_sorted_newest_first_and_has_paging_fields(seeded):
    res = await seeded.get("/api/logs")
    body = res.json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["page_size"] == 100
    assert body["pages"] == 1
    assert [i["timestamp"] for i in body["items"]] == [
        "2026-01-17T10:30:00",
        "2026-01-16T00:00:00",
        "2026-01-15T23:59:59",
        "2026-01-15T12:00:00",
        "2026-01-15T08:00:00",
    ]


async def test_list_items_leave_out_the_raw_line(seeded):
    item = (await seeded.get("/api/logs")).json()["items"][0]
    assert set(item) == {
        "id",
        "tenant",
        "log_type",
        "filename",
        "timestamp",
        "level",
        "logger",
        "iflow",
        "message",
        "ip",
        "node",
        "imported_at",
    }


async def test_single_entry_includes_the_raw_line(seeded):
    item = (
        await seeded.get("/api/logs", params={"grep": "all good", "date_from": "2026-01-15", "date_to": "2026-01-16"})
    ).json()["items"][0]
    entry = (await seeded.get(f"/api/logs/{item['id']}")).json()
    assert entry["raw_line"] == ROWS["a"][1]
    assert (await seeded.get("/api/logs/999999")).status_code == 404


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"tenant": "b"}, ["x", "refused again"]),
        ({"tenant": "all"}, ["x", "slow 50% done", "refused again", "all good", "connection refused"]),
        ({"level": "error"}, ["refused again", "connection refused"]),
        ({"level": "ERROR", "tenant": "a"}, ["connection refused"]),
        ({"level": "DEBUG"}, ["x"]),
        ({"level": "ALL"}, ["x", "slow 50% done", "refused again", "all good", "connection refused"]),
        ({"iflow": "Demo_A"}, ["slow 50% done", "connection refused"]),
        ({"iflow": "dEmO_a"}, ["slow 50% done", "connection refused"]),
        ({"iflow": "emo_"}, ["x", "slow 50% done", "refused again", "all good", "connection refused"]),
        ({"grep": "refused"}, ["refused again", "connection refused"]),
        ({"grep": "Special"}, ["x"]),  # logger is searched as well
        ({"grep": "SPECIAL"}, ["x"]),
        ({"grep": "REFUSED"}, ["refused again", "connection refused"]),
        # LIKE-style % / _ in the term act as wildcards (existing behavior).
        ({"grep": "50%"}, ["slow 50% done"]),
        ({"grep": "l_w"}, ["slow 50% done"]),
    ],
)
async def test_filters(seeded, params, expected):
    assert await messages(seeded, **params) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"date_from": "2026-01-16"}, ["x", "slow 50% done"]),
        ({"date_from": "2026-01-15 12:00:00"}, ["x", "slow 50% done", "refused again", "all good"]),
        # A bare date_to means the end of that day (23:59:59), inclusive.
        ({"date_to": "2026-01-15"}, ["refused again", "all good", "connection refused"]),
        ({"date_to": "2026-01-15 12:00:00"}, ["all good", "connection refused"]),
        ({"date_to": "2026-01-15T12:00:00"}, ["all good", "connection refused"]),
        ({"date_to": "2026-01-15T12:00"}, ["all good", "connection refused"]),
        ({"date_to": "2026-01-15T12:00:00Z"}, ["all good", "connection refused"]),
        ({"date_from": "2026-01-15 09:00:00", "date_to": "2026-01-16"}, ["slow 50% done", "refused again", "all good"]),
    ],
)
async def test_date_filters(seeded, params, expected):
    assert await messages(seeded, **params) == expected


async def test_offset_datetimes_are_normalized_to_utc(seeded):
    assert await messages(
        seeded,
        date_from="2026-01-15T09:00:00+01:00",
        date_to="2026-01-15T10:00:00+02:00",
    ) == ["connection refused"]


async def test_inverted_datetime_range_is_rejected(seeded):
    response = await seeded.get("/api/logs", params={"date_from": "2026-01-16", "date_to": "2026-01-15"})
    assert response.status_code == 422
    assert "date_from" in response.text


@pytest.mark.parametrize(
    "value", ["yesterday", "2026-13-01", "15.01.2026", "2026-01-15 25:00:00", "2026-W01-1", "20260115"]
)
async def test_invalid_dates_are_rejected_with_422(seeded, value):
    for field in ("date_from", "date_to"):
        res = await seeded.get("/api/logs", params={field: value})
        assert res.status_code == 422
        assert field in res.json()["detail"]


async def test_paging(client, tmp_path):
    db = app_db()
    path = write_log(tmp_path / "p.log", numbered_lines(5))
    await importer.import_log_file(db, "p", "trace", path, "p.log", 0)
    res = (await client.get("/api/logs", params={"page": 2, "page_size": 2})).json()
    assert res["total"] == 5
    assert res["pages"] == 3
    assert [i["message"] for i in res["items"]] == ["msg 2", "msg 1"]
    last = (await client.get("/api/logs", params={"page": 3, "page_size": 2})).json()
    assert [i["message"] for i in last["items"]] == ["msg 0"]
    beyond = (await client.get("/api/logs", params={"page": 9, "page_size": 2})).json()
    assert beyond["items"] == []


@pytest.mark.parametrize(
    "params", [{"page": 0}, {"page_size": 0}, {"page_size": 501}, {"page": 1_000_001}, {"page": "x"}]
)
async def test_paging_bounds(client, params):
    assert (await client.get("/api/logs", params=params)).status_code == 422


async def test_empty_database(client):
    body = (await client.get("/api/logs")).json()
    assert body == {"total": 0, "page": 1, "page_size": 100, "pages": 1, "items": []}


async def test_text_search_defaults_to_the_last_24_hours(client, tmp_path):
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    stale = (now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    path = write_log(
        tmp_path / "window.log",
        [log_line(ts=stale, message="needle old"), log_line(ts=recent, message="needle recent")],
    )
    await importer.import_log_file(app_db(), "a", "trace", path, path.name, 0)

    response = await client.get("/api/logs", params={"grep": "needle"})
    assert response.status_code == 200
    assert [row["message"] for row in response.json()["items"]] == ["needle recent"]


async def test_iflow_suggestions_keep_exact_full_names(seeded):
    response = await seeded.get("/api/logs/iflows")
    assert response.status_code == 200
    assert response.json()["items"] == ["Demo_A", "Demo_B", "Demo_C"]


async def test_iflow_suggestion_is_not_truncated(seeded, tmp_path):
    exact_name = "Integration_Flow_With_A_Long_Exact_Name"
    path = write_log(
        tmp_path / "long-name.log",
        [log_line(thread=f"1-{exact_name}_Worker-1", message="long name")],
    )
    await importer.import_log_file(app_db(), "a", "trace", path, path.name, 0)

    response = await seeded.get("/api/logs/iflows", params={"tenant": "a"})
    assert exact_name in response.json()["items"]


async def test_entries_with_the_same_timestamp_are_paged_by_id(client, tmp_path):
    db = app_db()
    path = write_log(tmp_path / "same.log", [log_line(ts="2026-01-15 08:00:00", message=f"m{i}") for i in range(7)])
    await importer.import_log_file(db, "a", "trace", path, path.name, 0)
    pages = [(await client.get("/api/logs", params={"page": p, "page_size": 3})).json()["items"] for p in (1, 2, 3)]
    ids = [item["id"] for page in pages for item in page]
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 7
