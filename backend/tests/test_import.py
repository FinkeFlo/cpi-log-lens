"""Incremental import: rows past the offset stored in file_imports are inserted, and
the offset is committed together with the rows."""

import gzip

import pytest

from app.repositories import file_imports as file_imports_repo
from app.services import importer, stats
from tests.support import log_line, numbered_lines, write_log

pytestmark = pytest.mark.anyio


async def count(db, sql, params=None):
    return await db.read(db.fetch_val, sql, params)


async def file_import(db, filename="a.log", log_type="trace"):
    return await file_imports_repo.get_file_import(db, "t1", log_type, filename)


async def test_new_file_imports_all_rows_and_records_lines_and_size(db, tmp_path):
    path = write_log(tmp_path / "a.log", numbered_lines(5))
    inserted = await importer.import_log_file(db, "t1", "trace", path, "a.log", 0, 1234)
    assert inserted == 5
    assert await count(db, "SELECT count(*) FROM logs") == 5
    assert await file_import(db) == {"lines": 5, "size": 1234}


async def test_grown_file_imports_only_the_new_rows(db, tmp_path):
    path = write_log(tmp_path / "a.log", numbered_lines(3))
    await importer.import_log_file(db, "t1", "trace", path, "a.log", 0, 100)
    write_log(path, numbered_lines(7))
    already = (await file_import(db))["lines"]
    inserted = await importer.import_log_file(db, "t1", "trace", path, "a.log", already, 200)
    assert inserted == 4
    messages = await db.read(db.fetch_all, "SELECT message FROM logs ORDER BY timestamp")
    assert [m["message"] for m in messages] == [f"msg {i}" for i in range(7)]
    # The offset is the total number of rows of the file, not the delta.
    assert await file_import(db) == {"lines": 7, "size": 200}


async def test_reimporting_an_unchanged_file_inserts_nothing(db, tmp_path):
    path = write_log(tmp_path / "a.log", numbered_lines(3))
    await importer.import_log_file(db, "t1", "trace", path, "a.log", 0, 100)
    inserted = await importer.import_log_file(db, "t1", "trace", path, "a.log", 3, 100)
    assert inserted == 0
    assert await count(db, "SELECT count(*) FROM logs") == 3


async def test_repeated_identical_lines_are_all_kept(db, tmp_path):
    line = log_line(message="heartbeat")
    path = write_log(tmp_path / "a.log", [line, line, line])
    assert await importer.import_log_file(db, "t1", "trace", path, "a.log", 0) == 3


async def test_offset_is_committed_per_batch(db, tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "INSERT_BATCH_SIZE", 2)
    path = write_log(tmp_path / "a.log", numbered_lines(5))
    assert await importer.import_log_file(db, "t1", "trace", path, "a.log", 0, 50) == 5
    assert await file_import(db) == {"lines": 5, "size": 50}


async def test_truncated_gzip_fails_and_keeps_the_file_retryable(db, tmp_path):
    data = gzip.compress(("\n".join(numbered_lines(3000)) + "\n").encode())
    path = tmp_path / "a.log"
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(EOFError):
        await importer.import_log_file(db, "t1", "trace", path, "a.log", 0, 999)
    # size stays 0, so the next fetch downloads and imports the file again.
    assert (await file_import(db))["size"] == 0


async def test_unparsed_lines_are_stored_on_the_first_import_only(db, tmp_path):
    path = write_log(tmp_path / "a.log", ["garbage", *numbered_lines(2)])
    await importer.import_log_file(db, "t1", "trace", path, "a.log", 0)
    write_log(path, ["garbage", *numbered_lines(4)])
    await importer.import_log_file(db, "t1", "trace", path, "a.log", 2)
    rows = await db.read(db.fetch_all, "SELECT tenant, log_type, filename, line_no, raw_text FROM unparsed_lines")
    assert rows == [{"tenant": "t1", "log_type": "trace", "filename": "a.log", "line_no": 1, "raw_text": "garbage"}]


async def test_unparsed_lines_of_a_file_without_rows_are_not_duplicated(db, tmp_path):
    path = write_log(tmp_path / "a.log", ["garbage 1", "garbage 2"])
    for _ in range(2):
        already = (await file_import(db))["lines"]
        await importer.import_log_file(db, "t1", "trace", path, "a.log", already, 77)
    assert await count(db, "SELECT count(*) FROM unparsed_lines") == 2
    # Read to the end: the size is recorded, so the next fetch skips the file.
    assert await file_import(db) == {"lines": 0, "size": 77}


async def test_same_file_name_for_two_log_types_is_imported_for_both(db, tmp_path):
    path = write_log(tmp_path / "a.log", numbered_lines(3))
    for log_type in ("trace", "http"):
        already = (await file_import(db, log_type=log_type))["lines"]
        await importer.import_log_file(db, "t1", log_type, path, "a.log", already)
    assert await count(db, "SELECT count(*) FROM logs WHERE log_type = 'http'") == 3
    assert await count(db, "SELECT count(*) FROM logs WHERE log_type = 'trace'") == 3
    assert await file_import(db, log_type="http") == {"lines": 3, "size": 0}


async def test_imports_invalidate_the_stats_cache(db, tmp_path):
    assert (await stats.get_stats(db))["total"] == 0
    path = write_log(tmp_path / "a.log", numbered_lines(2))
    await importer.import_log_file(db, "t1", "trace", path, "a.log", 0)
    assert (await stats.get_stats(db))["total"] == 2
