"""Parser: CPI log file -> rows (tenant, log_type, filename, timestamp, level, logger, iflow,
message, ip, node, raw_line) and unparsed lines."""

import gzip

import pytest

from app.db import _extract_iflow, iter_log_batches
from tests.support import log_line, numbered_lines, write_log


def parse(path, batch_size=20000):
    batches = list(iter_log_batches("t1", "trace", path, batch_size))
    rows = [r for b, _ in batches for r in b]
    unparsed = [line for _, lines in batches for line in lines]
    return batches, rows, unparsed


def test_valid_line_yields_all_fields(tmp_path):
    line = log_line(
        ts="2026-01-15 08:00:20",
        level=" WARN ",
        logger=" com.example.Worker ",
        thread="1746228855278-Demo_Order_to_S4_Worker-1",
        message=" order 42 failed ",
        ip=" 198.51.100.25 ",
        node=" 6",
    )
    _, rows, unparsed = parse(write_log(tmp_path / "a.log", [line]))
    assert unparsed == []
    assert rows == [
        (
            "t1",
            "trace",
            "a.log",
            "2026-01-15 08:00:20",
            "WARN",
            "com.example.Worker",
            "Demo_Order_to_S4",
            "order 42 failed",
            "198.51.100.25",
            "6",
            line,
        )
    ]


@pytest.mark.parametrize(
    ("thread", "iflow"),
    [
        ("Camel (Demo_Flow) thread 35 - timer://Demo_Flow", "Demo_Flow"),
        ("scheduler-Demo_Poll_Worker-1", "Demo_Poll"),
        ("1737238118820-Demo_Order_to_S4_Worker-3", "Demo_Order_to_S4"),
        ("123-Demo_Plain", "Demo_Plain"),
        ("http-nio-8080-exec-5", "http-nio-8080-exec-5"),
        ("  padded  ", "padded"),
    ],
)
def test_iflow_is_extracted_from_the_thread_name(thread, iflow):
    assert _extract_iflow(thread) == iflow


def test_continuation_lines_are_appended_to_message_and_raw_line(tmp_path):
    first = log_line(ts="2026-01-15 08:00:00", message="boom")
    second = log_line(ts="2026-01-15 08:00:01", message="next")
    trace = ["java.lang.RuntimeException: x", "\tat com.example.A.b(A.java:1)"]
    _, rows, unparsed = parse(write_log(tmp_path / "a.log", [first, *trace, second]))
    assert unparsed == []
    assert len(rows) == 2
    assert rows[0][7] == "boom\n" + "\n".join(trace)
    assert rows[0][10] == first + "\n" + "\n".join(trace)
    assert rows[1][7] == "next"


def test_unparsable_lines_before_the_first_row_are_returned_with_line_numbers(tmp_path):
    lines = ["garbage one", "", "garbage three", log_line(message="ok")]
    _, rows, unparsed = parse(write_log(tmp_path / "a.log", lines))
    assert [r[7] for r in rows] == ["ok"]
    assert unparsed == [(1, "garbage one"), (2, ""), (3, "garbage three")]


def test_continuations_spanning_a_batch_boundary_stay_attached(tmp_path):
    lines = [
        log_line(ts="2026-01-15 08:00:00", message="a"),
        log_line(ts="2026-01-15 08:00:01", message="b"),
        "  continuation of b",
        "  more of b",
        log_line(ts="2026-01-15 08:00:02", message="c"),
    ]
    batches, rows, _ = parse(write_log(tmp_path / "a.log", lines), batch_size=2)
    # A row is only emitted once the next row starts, so "b" keeps both continuation lines.
    assert [len(b) for b, _ in batches] == [2, 1]
    assert [r[7] for r in rows] == ["a", "b\n  continuation of b\n  more of b", "c"]


def test_batches_have_the_requested_size(tmp_path):
    batches, rows, _ = parse(write_log(tmp_path / "a.log", numbered_lines(5)), batch_size=2)
    assert [len(b) for b, _ in batches] == [2, 2, 1]
    assert [r[7] for r in rows] == [f"msg {i}" for i in range(5)]


def test_gzip_is_detected_by_content_not_by_name(tmp_path):
    path = write_log(tmp_path / "plain-name.log", numbered_lines(3), gz=True)
    _, rows, _ = parse(path)
    assert [r[7] for r in rows] == ["msg 0", "msg 1", "msg 2"]
    assert rows[0][2] == "plain-name.log"


def test_truncated_gzip_raises_instead_of_returning_partial_data(tmp_path):
    data = gzip.compress(("\n".join(numbered_lines(2000)) + "\n").encode())
    path = tmp_path / "cut.log"
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(EOFError):
        parse(path)


def test_invalid_utf8_is_replaced(tmp_path):
    path = tmp_path / "a.log"
    path.write_bytes(log_line(message="caf\xe9").encode("latin-1") + b"\n")
    _, rows, _ = parse(path)
    assert rows[0][7] == "caf�"


def test_empty_file_yields_nothing(tmp_path):
    path = tmp_path / "empty.log"
    path.write_bytes(b"")
    assert parse(path) == ([], [], [])
