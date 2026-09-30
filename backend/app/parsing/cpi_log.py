"""Parser for CPI trace/HTTP log files.

A line has 15 '#'-separated fields:
Timestamp # Timezone # Level # Logger # User # Thread # Category # … # Message # - # IP # Node
"""

import gzip
import re
from collections.abc import Iterator
from pathlib import Path

# Order of the values in a parsed row (and of the columns they are inserted into).
ROW_COLUMNS = (
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
    "raw_line",
)
_MESSAGE, _RAW_LINE = ROW_COLUMNS.index("message"), ROW_COLUMNS.index("raw_line")

Row = tuple[str, ...]  # values in ROW_COLUMNS order
UnparsedLine = tuple[int, str]  # (line number, text)

LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})#[^#]*#([^#]*)#([^#]*)#[^#]*#([^#]*)#[^#]*"
    r"#[^#]*#[^#]*#[^#]*#[^#]*#(.*?)#-#([^#]*)#([^\n]*)$"
)

_IFLOW_RE = re.compile(
    r"Camel \(([^)]+)\)"
    r"|(?:scheduler-|\d+-)"
    r"(.+?)(?:_Worker.*)?$"
)


def extract_iflow(thread: str) -> str:
    """IFlow name from the thread field, e.g. 'Camel (X) thread 3 - …', 'scheduler-X_Worker-1', '123-X_Worker-2'."""
    thread = thread.strip()
    m = _IFLOW_RE.match(thread)
    if m:
        return (m.group(1) or m.group(2) or thread).strip()
    return thread


def is_gzip(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except Exception:
        return False


def iter_log_batches(
    tenant: str, log_type: str, filepath: Path, batch_size: int = 20000
) -> Iterator[tuple[list[Row], list[UnparsedLine]]]:
    """Parse a CPI log file into structured rows, in batches of `batch_size`.

    Yields (rows, unparsed):
    - rows: tuples in ROW_COLUMNS order (tenant, log_type, filename,
      timestamp, level, logger, iflow, message, ip, node, raw_line).
    - unparsed: (line_no, raw_text) for lines that could not be matched
      against LINE_RE *and* had no preceding parsed row in this file to
      attach to — kept so nothing is silently dropped.

    Lines that don't match LINE_RE (e.g. a stacktrace continuation of a
    multi-line log message) are appended to the message/raw_line of the
    previously parsed row. A row is only emitted once the next row (or the
    end of the file) is seen, so continuations that span a batch boundary
    stay attached to their row. Memory stays bounded by one batch, however
    large the file is.

    NOTE on incremental re-fetch bookkeeping (see import_log_file /
    file_imports): the file's whole content is re-parsed on every fetch, and
    only rows past the offset stored in file_imports.lines are imported — that
    offset counts *top-level rows* (post-merge), not raw physical lines. This
    is safe for the normal case (a file only ever grows by new complete lines
    at the end). Edge case: if a multi-line message is only partially written
    when a fetch runs and more continuation lines for that *same* message
    appear by the next fetch, those extra lines won't be picked up — the row
    they belong to is already before the offset. Considered acceptable: rare,
    and the message is still captured (possibly truncated) rather than
    duplicated or corrupted.
    """
    batch: list[Row] = []
    unparsed: list[UnparsedLine] = []
    current: list[str] | None = None
    opener = gzip.open if is_gzip(filepath) else open
    with opener(filepath, "rt", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            raw = line.rstrip("\n")
            m = LINE_RE.match(raw)
            if not m:
                if current is not None:
                    # Continuation line (e.g. stacktrace) — append to the
                    # current row's message and raw_line.
                    current[_MESSAGE] += "\n" + raw
                    current[_RAW_LINE] += "\n" + raw
                else:
                    unparsed.append((line_no, raw))
                continue
            if current is not None:
                batch.append(tuple(current))
                if len(batch) >= batch_size:
                    yield batch, unparsed
                    batch, unparsed = [], []
            ts, level, logger, iflow, message, ip, node = m.groups()
            current = [
                tenant,
                log_type,
                filepath.name,
                ts,
                level.strip(),
                logger.strip(),
                extract_iflow(iflow),
                message.strip(),
                ip.strip(),
                node.strip(),
                raw,
            ]
    if current is not None:
        batch.append(tuple(current))
    if batch or unparsed:
        yield batch, unparsed
