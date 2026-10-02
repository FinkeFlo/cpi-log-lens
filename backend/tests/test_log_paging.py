"""Cursor paging and jump to time in the log list (GET /api/logs)."""

import asyncio
from datetime import datetime

import pytest

from app.repositories.database import READ_POOL_SIZE
from app.repositories.logs import MAX_ENTRY_ID, Cursor
from app.services import importer
from tests.support import app_db, log_line, numbered_lines, write_log

pytestmark = pytest.mark.anyio


async def import_lines(tmp_path, lines, tenant="p", name="p.log"):
    path = write_log(tmp_path / name, lines)
    await importer.import_log_file(app_db(), tenant, "trace", path, name, 0)


async def get(client, **params):
    res = await client.get("/api/logs", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def messages(body):
    return [item["message"] for item in body["items"]]


@pytest.fixture
async def ten(client, tmp_path):
    """msg 0 … msg 9, one minute apart from 2026-01-15 08:00:00."""
    await import_lines(tmp_path, numbered_lines(10))
    return client


def test_the_entries_newer_than_a_position():
    ts = datetime(2026, 1, 15, 8, 0)
    assert Cursor("o", ts, 5).boundary() == Cursor("n", ts, 4)
    assert Cursor("o", ts, 0).boundary() == Cursor("n", datetime(2026, 1, 15, 7, 59, 59, 999999), MAX_ENTRY_ID)
    assert Cursor("a", ts, 5).boundary() == Cursor("n", ts, 5)
    assert Cursor.at(ts).boundary() == Cursor("n", ts, MAX_ENTRY_ID)


def test_cursor_text_round_trip():
    for cursor in (
        Cursor("o", datetime(2026, 9, 30, 11, 59, 58), 12345),
        Cursor("n", datetime(2026, 9, 30, 11, 59, 58, 120000), 1),
        Cursor("a", datetime(1, 1, 1), MAX_ENTRY_ID),
    ):
        assert Cursor.parse(str(cursor)) == cursor
    assert str(Cursor("o", datetime(2026, 9, 30, 11, 59, 58), 7)) == "o20260930T115958_7"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "x20260930T115958_1",
        "o20260930T115958",
        "o20260230T000000_1",
        "o20260930T115958_9999999999999999999",
        "o2026-09-30T11:59:58_1",
        "o20260930T115958_1 ",
    ],
)
def test_invalid_cursor_text(text):
    with pytest.raises(ValueError):
        Cursor.parse(text)


async def test_older_and_newer_cursors_walk_the_whole_list(ten):
    first = await get(ten, page_size=3)
    assert messages(first) == ["msg 9", "msg 8", "msg 7"]
    assert (first["total"], first["offset"], first["newer_cursor"]) == (10, 0, None)

    pages, body = [first], first
    while body["older_cursor"]:
        body = await get(ten, page_size=3, cursor=body["older_cursor"])
        pages.append(body)
    assert [messages(p) for p in pages] == [
        ["msg 9", "msg 8", "msg 7"],
        ["msg 6", "msg 5", "msg 4"],
        ["msg 3", "msg 2", "msg 1"],
        ["msg 0"],
    ]
    assert [p["offset"] for p in pages] == [0, 3, 6, 9]
    assert all(p["total"] == 10 for p in pages)
    assert all(p["newer_cursor"] for p in pages[1:])

    back = await get(ten, page_size=3, cursor=pages[3]["newer_cursor"])
    assert messages(back) == ["msg 3", "msg 2", "msg 1"]
    assert back["offset"] == 6
    back = await get(ten, page_size=3, cursor=back["newer_cursor"])
    assert messages(back) == ["msg 6", "msg 5", "msg 4"]


async def test_newer_than_less_than_a_page_shows_the_newest_page(ten):
    second = await get(ten, page_size=4, cursor=(await get(ten, page_size=2))["older_cursor"])
    assert messages(second) == ["msg 7", "msg 6", "msg 5", "msg 4"]
    newer = await get(ten, page_size=4, cursor=second["newer_cursor"])
    assert messages(newer) == ["msg 9", "msg 8", "msg 7", "msg 6"]
    assert (newer["offset"], newer["newer_cursor"]) == (0, None)


async def test_without_count_total_and_offset_are_left_out(ten):
    first = await get(ten, page_size=3, count="false")
    assert (first["total"], first["pages"], first["offset"]) == (None, None, 0)
    second = await get(ten, page_size=3, cursor=first["older_cursor"], count="false")
    assert messages(second) == ["msg 6", "msg 5", "msg 4"]
    assert (second["total"], second["offset"]) == (None, None)
    assert second["newer_cursor"] and second["older_cursor"]
    last = await get(ten, page_size=9, cursor=second["older_cursor"], count="false")
    assert messages(last) == ["msg 3", "msg 2", "msg 1", "msg 0"]
    assert last["older_cursor"] is None
    top = await get(ten, page_size=2, cursor=(await get(ten, page_size=2))["older_cursor"], count="false")
    newest = await get(ten, page_size=2, cursor=top["newer_cursor"], count="false")
    assert messages(newest) == ["msg 9", "msg 8"]
    assert newest["newer_cursor"] is None


async def test_entries_with_one_timestamp_are_paged_by_id(client, tmp_path):
    await import_lines(tmp_path, [log_line(ts="2026-01-15 08:00:00", message=f"m{i}") for i in range(7)])
    body, ids = await get(client, page_size=3), []
    while True:
        ids += [item["id"] for item in body["items"]]
        if not body["older_cursor"]:
            break
        body = await get(client, page_size=3, cursor=body["older_cursor"])
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 7


async def test_cursor_pages_keep_the_filters(client, tmp_path):
    lines = [
        log_line(ts=f"2026-01-15 08:0{i}:00", level="ERROR" if i % 2 else "INFO", message=f"m{i}") for i in range(8)
    ]
    await import_lines(tmp_path, lines)
    first = await get(client, level="ERROR", page_size=2)
    assert messages(first) == ["m7", "m5"]
    second = await get(client, level="ERROR", page_size=2, cursor=first["older_cursor"])
    assert messages(second) == ["m3", "m1"]
    assert (second["total"], second["offset"], second["older_cursor"]) == (4, 2, None)


async def test_jump_to_time_starts_at_the_newest_entry_at_or_before_it(ten):
    body = await get(ten, page_size=3, at="2026-01-15T08:05:30")
    assert messages(body) == ["msg 5", "msg 4", "msg 3"]
    assert (body["total"], body["offset"]) == (10, 4)
    assert messages(await get(ten, page_size=3, cursor=body["newer_cursor"])) == ["msg 8", "msg 7", "msg 6"]
    assert messages(await get(ten, page_size=3, at="2026-01-15 08:05:00")) == ["msg 5", "msg 4", "msg 3"]
    # timezone offsets are converted to UTC; a bare date means the end of that day
    assert messages(await get(ten, page_size=1, at="2026-01-15T10:02:00+02:00")) == ["msg 2"]
    assert messages(await get(ten, page_size=1, at="2026-01-15")) == ["msg 9"]
    after_all = await get(ten, page_size=3, at="2026-02-01T00:00:00")
    assert (messages(after_all), after_all["newer_cursor"]) == (["msg 9", "msg 8", "msg 7"], None)


async def test_jump_before_the_first_entry_offers_the_oldest_entries(ten):
    for count in ("true", "false"):
        body = await get(ten, page_size=3, at="2026-01-01T00:00:00", count=count)
        assert body["items"] == [] and body["older_cursor"] is None
        assert body["total"] == (10 if count == "true" else None)
        oldest = await get(ten, page_size=3, cursor=body["newer_cursor"])
        assert messages(oldest) == ["msg 2", "msg 1", "msg 0"]
        assert oldest["older_cursor"] is None


async def test_a_cursor_whose_entries_were_deleted_is_an_empty_page(ten):
    cursor = (await get(ten, page_size=7))["older_cursor"]
    assert messages(await get(ten, page_size=3, cursor=cursor)) == ["msg 2", "msg 1", "msg 0"]
    await app_db().run(lambda conn: conn.execute("DELETE FROM logs WHERE message IN ('msg 0', 'msg 1', 'msg 2')"))

    empty = await get(ten, page_size=3, cursor=cursor)
    assert (empty["items"], empty["total"], empty["offset"], empty["older_cursor"]) == ([], 7, None, None)
    assert messages(await get(ten, page_size=3, cursor=empty["newer_cursor"])) == ["msg 5", "msg 4", "msg 3"]


async def test_page_numbers_still_work_and_continue_with_cursors(ten):
    second = await get(ten, page=2, page_size=3)
    assert messages(second) == ["msg 6", "msg 5", "msg 4"]
    assert (second["total"], second["page"], second["pages"], second["offset"]) == (10, 2, 4, 3)
    assert messages(await get(ten, page_size=3, cursor=second["older_cursor"])) == ["msg 3", "msg 2", "msg 1"]
    assert messages(await get(ten, page_size=3, cursor=second["newer_cursor"])) == ["msg 9", "msg 8", "msg 7"]
    beyond = await get(ten, page=9, page_size=3)
    assert (beyond["items"], beyond["newer_cursor"], beyond["older_cursor"]) == ([], None, None)


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"page": 2, "cursor": "o20260115T080000_1"}, "only one of"),
        ({"at": "2026-01-15", "cursor": "o20260115T080000_1"}, "only one of"),
        ({"cursor": "nope"}, "cursor is invalid"),
        ({"cursor": "o20260231T080000_1"}, "cursor is invalid"),
        ({"at": "yesterday"}, "at must be"),
    ],
)
async def test_invalid_positions_are_rejected_with_422(ten, params, detail):
    res = await ten.get("/api/logs", params=params)
    assert res.status_code == 422
    assert detail in str(res.json()["detail"])


async def test_the_list_answers_503_when_no_read_cursor_is_free(ten, monkeypatch, settings, caplog):
    # The page query and the count wait for a read cursor each; the first that gives up
    # fails the request and stops the other one, and every cursor goes back to the pool.
    monkeypatch.setattr(settings, "query_timeout_s", 1.0)
    monkeypatch.setattr(settings, "pool_acquire_timeout_s", 0.1)
    db = app_db()
    slow_query = "SELECT count(*) FROM range(100000000000) t(i) WHERE i % 7 = 3"
    slow = [asyncio.create_task(db.read(db.fetch_val, slow_query)) for _ in range(READ_POOL_SIZE)]
    await asyncio.sleep(0.05)
    res = await ten.get("/api/logs", params={"page_size": 3})
    assert res.status_code == 503
    await asyncio.gather(*slow, return_exceptions=True)
    # An interrupted query hands its cursor back once it has actually stopped.
    for _ in range(100):
        if db._read_pool.qsize() == READ_POOL_SIZE:
            break
        await asyncio.sleep(0.02)
    assert db._read_pool.qsize() == READ_POOL_SIZE
    assert messages(await get(ten, page_size=3)) == ["msg 9", "msg 8", "msg 7"]
    assert "never retrieved" not in caplog.text
