"""Log list filters and paging (GET /api/logs)."""

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
    item = (await seeded.get("/api/logs", params={"grep": "all good"})).json()["items"][0]
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
        ({"level": "ALL"}, ["x", "slow 50% done", "refused again", "all good", "connection refused"]),
        ({"iflow": "Demo_A"}, ["slow 50% done", "connection refused"]),
        ({"iflow": "emo_"}, ["x", "slow 50% done", "refused again", "all good", "connection refused"]),
        ({"grep": "refused"}, ["refused again", "connection refused"]),
        ({"grep": "Special"}, ["x"]),  # logger is searched as well
        # LIKE is case-sensitive and % / _ in the term act as wildcards (current behaviour).
        ({"grep": "REFUSED"}, []),
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


@pytest.mark.parametrize("value", ["yesterday", "2026-13-01", "15.01.2026", "2026-01-15 25:00:00"])
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
