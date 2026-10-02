"""Log entries: bulk insert, list/detail queries, statistics and deletion."""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pyarrow as pa

from app.parsing.cpi_log import ROW_COLUMNS, Row, UnparsedLine
from app.repositories.database import Database

_COLUMNS = list(ROW_COLUMNS)


def insert_rows(conn, rows: list[Row]) -> None:
    # Bulk-insert via a registered pyarrow Table + `INSERT INTO ... SELECT`,
    # instead of a parameterized multi-row `INSERT ... VALUES (?,?,...)`.
    # Benchmarked against a copy of the real (6M-row) production table: the
    # parameterized-VALUES approach costs ~1.2 ms/row *independent of table
    # size or batch size* (pure Python->DuckDB parameter-binding overhead for
    # the ~10 * batch_size individual `?` values) — for a session importing
    # ~1M rows that alone is ~20 minutes of pure CPU-bound marshalling. The
    # pyarrow path measured ~0.0015 ms/row on the same data (~800-1000x
    # faster), because DuckDB ingests the whole batch as a columnar buffer in
    # one call instead of binding each value individually.
    columns = list(zip(*rows, strict=True))
    arrow_table = pa.table(
        {col: pa.array(values, type=pa.string()) for col, values in zip(_COLUMNS, columns, strict=True)}
    )
    conn.register("_import_batch", arrow_table)
    try:
        conn.execute(f"INSERT INTO logs ({','.join(_COLUMNS)}) SELECT {','.join(_COLUMNS)} FROM _import_batch")
    finally:
        conn.unregister("_import_batch")


def replace_unparsed(conn, tenant: str, log_type: str, filename: str, lines: list[UnparsedLine]) -> None:
    """Store the unparsable lines at the start of a file, replacing earlier ones of the same file."""
    conn.execute(
        "DELETE FROM unparsed_lines WHERE tenant = ? AND log_type = ? AND filename = ?", [tenant, log_type, filename]
    )
    conn.executemany(
        """
        INSERT INTO unparsed_lines (tenant, log_type, filename, line_no, raw_text)
        VALUES (?, ?, ?, ?, ?)
    """,
        [[tenant, log_type, filename, n, t] for n, t in lines],
    )


# ── Query ─────────────────────────────────────────────────────────────────────

# Columns of the log list. raw_line (the full original line, often as large
# as the message again) is left out: it is only shown in the detail view,
# which loads it via get_log_entry(). Reading it for every listed row
# roughly doubled the data each list query had to scan and transfer.
_LIST_COLUMNS = "id, tenant, log_type, filename, timestamp, level, logger, iflow, message, ip, node, imported_at"
TEXT_SEARCH_DEFAULT_HOURS = 24
# CPI timestamps have whole seconds, so many entries share one. The id breaks the tie:
# without it, entries with the same timestamp came back in any order, and a page boundary
# between them could show an entry twice or skip it.
_NEWEST_FIRST = "ORDER BY timestamp DESC, id DESC"


def effective_date_bounds(
    *,
    grep: str | None,
    date_from: str | None,
    date_to: str | None,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    """Bound text searches to 24 hours unless the caller supplies both bounds."""
    if not grep or (date_from is not None and date_to is not None):
        return date_from, date_to

    current = now or datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    if date_to is not None:
        end = datetime.fromisoformat(date_to)
        if len(date_to) == 10:
            end = end.replace(hour=23, minute=59, second=59)
        start = end - timedelta(hours=TEXT_SEARCH_DEFAULT_HOURS)
    else:
        end = current
        start = datetime.fromisoformat(date_from) if date_from else end - timedelta(hours=TEXT_SEARCH_DEFAULT_HOURS)

    return start.isoformat(sep=" "), date_to or end.isoformat(sep=" ")


async def list_iflows(db: Database, tenant: str | None = None) -> list[str]:
    def _run(cur):
        where = "WHERE iflow IS NOT NULL AND iflow <> ''"
        params: list[str] = []
        if tenant and tenant != "all":
            where += " AND tenant = ?"
            params.append(tenant)
        rows = db.fetch_all(
            cur, f"SELECT DISTINCT iflow FROM logs {where} ORDER BY lower(iflow), iflow", params or None
        )
        return [row["iflow"] for row in rows]

    return await db.read(_run)


def _list_conditions(
    *,
    tenant: str | None,
    level: str | None,
    iflow: str | None,
    grep: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[list[str], list]:
    """WHERE conditions (joined with AND) and their parameters for the log list filters."""
    conditions: list[str] = []
    params: list = []
    if tenant and tenant != "all":
        conditions.append("tenant = ?")
        params.append(tenant)
    if level and level != "ALL":
        conditions.append("upper(level) = ?")
        params.append(level.upper())
    if iflow:
        conditions.append("iflow ILIKE ?")
        params.append(f"%{iflow}%")
    if grep:
        conditions.append("(message ILIKE ? OR logger ILIKE ?)")
        params.extend([f"%{grep}%", f"%{grep}%"])
    if date_from:
        conditions.append("timestamp >= CAST(? AS TIMESTAMP)")
        params.append(date_from)
    if date_to:
        conditions.append("timestamp <= CAST(? AS TIMESTAMP)")
        # date_to accepts either a bare date ("YYYY-MM-DD", as sent by the
        # Browse UI's <input type="date">) or a full datetime ("YYYY-MM-DD
        # HH:MM:SS", per the /api/query contract). A bare date is expanded
        # to the end of that day; a full datetime is used as-is — blindly
        # appending " 23:59:59" to an already-complete datetime produced
        # an invalid TIMESTAMP string (e.g. "... 00:00:00 23:59:59") and a
        # 500 error for every /api/query call that passed a full datetime.
        params.append(date_to if len(date_to) > 10 else date_to + " 23:59:59")
    return conditions, params


# ── Log list positions ───────────────────────────────────────────────────────

MAX_ENTRY_ID = 2**63 - 1  # largest BIGINT: a cursor with it stands for the end of its timestamp
_CURSOR_RE = re.compile(r"([ona])([0-9]{8}T[0-9]{6}(?:\.[0-9]{1,6})?)_([0-9]{1,19})")  # ASCII digits only


@dataclass(frozen=True)
class Cursor:
    """A position in the newest-first log list, relative to the entry (timestamp, id):
    "o" = the entries older than it, "n" = the entries newer than it, "a" = the entry
    and the older ones. As text: the kind, the compact ISO timestamp and the id, e.g.
    "o20260930T115958_12345". Shared Browse links contain cursors, so the format stays."""

    kind: Literal["o", "n", "a"]
    timestamp: datetime
    id: int

    @classmethod
    def parse(cls, text: str) -> "Cursor":
        m = _CURSOR_RE.fullmatch(text)
        if not m:
            raise ValueError("cursor is invalid")
        stamp, entry_id = m.group(2), int(m.group(3))
        timestamp = datetime.strptime(stamp, "%Y%m%dT%H%M%S.%f" if "." in stamp else "%Y%m%dT%H%M%S")
        # Entry ids start at 1, so an "o" cursor (always made from an entry) has one of
        # at least 1; "n" cursors may have 0 (see boundary()).
        if entry_id > MAX_ENTRY_ID or (m.group(1) == "o" and entry_id < 1):
            raise ValueError("cursor is invalid")
        kind: Literal["o", "n", "a"] = m.group(1)  # type: ignore[assignment]
        return cls(kind, timestamp, entry_id)

    @classmethod
    def at(cls, timestamp: datetime) -> "Cursor":
        """The newest entry at or before `timestamp` and the older ones."""
        return cls("a", timestamp, MAX_ENTRY_ID)

    def __str__(self) -> str:
        t = self.timestamp
        text = f"{t.year:04d}{t.month:02d}{t.day:02d}T{t.hour:02d}{t.minute:02d}{t.second:02d}"
        if t.microsecond:
            text += f".{t.microsecond:06d}"
        return f"{self.kind}{text}_{self.id}"

    def boundary(self) -> "Cursor":
        """The entries newer than the cursor's position: for "o" its entry and the newer
        ones, for "a" and "n" the entries newer than its entry. For an "o" or "a" page,
        these are exactly the entries newer than the page."""
        return Cursor("n", self.timestamp, self.id - 1 if self.kind == "o" else self.id)

    def halves(self) -> tuple[tuple[str, list], tuple[str, list]]:
        """condition() in two parts without an OR: the entries of the cursor's timestamp
        on its side of the id, and the entries of the timestamps on its side."""
        if self.kind == "n":
            return ("timestamp = ? AND id > ?", [self.timestamp, self.id]), ("timestamp > ?", [self.timestamp])
        op = "<" if self.kind == "o" else "<="
        return (f"timestamp = ? AND id {op} ?", [self.timestamp, self.id]), ("timestamp < ?", [self.timestamp])

    def condition(self) -> tuple[str, list]:
        """SQL condition for the entries on this cursor's side, and its parameters. The
        plain timestamp bound is redundant, but lets DuckDB skip whole row groups by
        their min/max timestamp, which it can't do for the OR."""
        if self.kind == "n":
            return "timestamp >= ? AND (timestamp > ? OR id > ?)", [self.timestamp, self.timestamp, self.id]
        op = "<" if self.kind == "o" else "<="
        return f"timestamp <= ? AND (timestamp < ? OR id {op} ?)", [self.timestamp, self.timestamp, self.id]


def _entry_cursor(kind: Literal["o", "n", "a"], row: dict) -> Cursor:
    return Cursor(kind, row["timestamp"], row["id"])


_OLDEST_FIRST = "ORDER BY timestamp ASC, id ASC"


@dataclass
class _Page:
    items: list[dict]
    number: int | None  # page number, in page-number mode
    has_newer: bool | None  # None: not known without counting
    has_older: bool


async def query_logs(
    db: Database,
    *,
    tenant: str | None = None,
    level: str | None = None,
    iflow: str | None = None,
    grep: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int | None = None,
    cursor: Cursor | None = None,
    page_size: int = 100,
    count: bool = True,
) -> dict:
    """One page of the log list, newest first (by timestamp, then id).

    The page is chosen by number (`page`, LIMIT/OFFSET from the newest entry: the
    deeper, the slower) or by a `cursor` (keyset: equally fast at any depth); without
    either, it is the newest page. Every answer has `newer_cursor` and `older_cursor`
    for the neighbouring pages (null at the ends). `count` adds `total`, the number of
    matching entries, and `offset`, the number of matching entries newer than the
    page. Counting scans all matching entries, so it runs next to the page query on a
    second read cursor, and stepping to the next page can leave it out.
    """
    date_from, date_to = effective_date_bounds(grep=grep, date_from=date_from, date_to=date_to)
    conditions, params = _list_conditions(
        tenant=tenant, level=level, iflow=iflow, grep=grep, date_from=date_from, date_to=date_to
    )
    number = (page or 1) if cursor is None else None  # page-number mode
    boundary = cursor.boundary() if cursor is not None else None

    def where(*extra: str) -> str:
        parts = [*conditions, *extra]
        return ("WHERE " + " AND ".join(parts)) if parts else ""

    def exists(cur, side: Cursor) -> bool:
        cond, cond_params = side.condition()
        return db.fetch_val(cur, f"SELECT 1 FROM logs {where(cond)} LIMIT 1", [*params, *cond_params]) is not None

    def cursor_entry_matches(cur, c: Cursor) -> bool:
        """Whether the cursor's own entry is still there and matches (an index lookup)."""
        sql = f"SELECT 1 FROM logs {where('id = ?', 'timestamp = ?')}"
        return db.fetch_val(cur, sql, [*params, c.id, c.timestamp]) is not None

    # One entry more than a page tells whether there are more beyond it.
    def select(cur, side: Cursor | None, order: str, offset: int = 0) -> list[dict]:
        if side is None:
            sql = f"SELECT {_LIST_COLUMNS} FROM logs {where()} {order} LIMIT ? OFFSET ?"
            return db.fetch_all(cur, sql, [*params, page_size + 1, offset])
        # A query per half of the cursor's condition, merged. With the OR of both halves in
        # one query, DuckDB applied it only after reading every column of every entry that
        # passed the other filters: over 100 ms per page for an IFlow and a level matching
        # 14,000 of 20 million entries, against about 15 ms this way.
        (same_second, same_params), (other_seconds, other_params) = side.halves()
        sql = (
            f"SELECT * FROM ((SELECT {_LIST_COLUMNS} FROM logs {where(same_second)} {order} LIMIT ?) "
            f"UNION ALL (SELECT {_LIST_COLUMNS} FROM logs {where(other_seconds)} {order} LIMIT ?)) {order} LIMIT ?"
        )
        limit = page_size + 1
        return db.fetch_all(cur, sql, [*params, *same_params, limit, *params, *other_params, limit, limit])

    def read_page(cur) -> _Page:
        if cursor is not None and cursor.kind == "n":
            rows = select(cur, cursor, _OLDEST_FIRST)
            if len(rows) <= page_size:  # less than a page of newer entries: show the newest page
                rows = select(cur, None, _NEWEST_FIRST)
                return _Page(rows[:page_size], 1, False, len(rows) > page_size)
            items = rows[:page_size][::-1]
            # The cursor's entry is older than the page, unless it is gone (or the cursor
            # came from an empty page and has no entry).
            has_older = cursor_entry_matches(cur, cursor) or exists(cur, _entry_cursor("o", items[-1]))
            return _Page(items, None, True, has_older)
        if cursor is None:
            n = number or 1
            rows = select(cur, None, _NEWEST_FIRST, (n - 1) * page_size)
            items = rows[:page_size]
            return _Page(items, n, n > 1 and bool(items), len(rows) > page_size)
        rows = select(cur, cursor, _NEWEST_FIRST)
        has_newer = None
        if not count:  # else the count tells
            has_newer = (cursor.kind == "o" and cursor_entry_matches(cur, cursor)) or exists(cur, cursor.boundary())
        return _Page(rows[:page_size], None, has_newer, len(rows) > page_size)

    def read_count(cur) -> tuple[int, int | None]:
        """Matching entries, and how many of them are newer than the cursor's page."""
        if boundary is None:
            return db.fetch_val(cur, f"SELECT count(*) FROM logs {where()}", params or None) or 0, None
        cond, cond_params = boundary.condition()
        counts = db.fetch_one(
            cur,
            f"SELECT count(*) AS total, count(*) FILTER (WHERE {cond}) AS newer FROM logs {where()}",
            [*cond_params, *params],
        )
        assert counts is not None
        return counts["total"], counts["newer"]

    total = newer = None
    if count:
        tasks = [asyncio.ensure_future(db.read(read_page)), asyncio.ensure_future(db.read(read_count))]
        try:
            result, (total, newer) = await asyncio.gather(*tasks)
        except BaseException:
            # One failed (or the request was cancelled): stop the other one, too.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    else:
        result = await db.read(read_page)

    items = result.items
    offset = None
    if items and result.number is not None:
        offset = (result.number - 1) * page_size
    elif items and newer is not None:
        offset = max(0, newer - len(items)) if cursor is not None and cursor.kind == "n" else newer
    has_newer = result.has_newer if result.has_newer is not None else bool(newer)
    if not has_newer:
        newer_cursor = None
    elif items:
        newer_cursor = str(_entry_cursor("n", items[0]))
    else:
        newer_cursor = str(boundary) if boundary else None
    return {
        "total": total,
        "page": result.number,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size) if result.number and total is not None else None,
        "offset": offset,
        "newer_cursor": newer_cursor,
        "older_cursor": str(_entry_cursor("o", items[-1])) if result.has_older else None,
        "items": items,
    }


async def get_log_entry(db: Database, entry_id: int) -> dict | None:
    return await db.read(db.fetch_one, "SELECT * FROM logs WHERE id=?", [entry_id])


# ── Statistics ────────────────────────────────────────────────────────────────


async def compute_stats(db: Database, tenant: str | None = None) -> dict:
    def _run(cur):
        where = "WHERE tenant=?" if tenant and tenant != "all" else ""
        params = [tenant] if tenant and tenant != "all" else []

        levels = db.fetch_all(
            cur,
            f"SELECT upper(level) as lvl, COUNT(*) as cnt FROM logs {where} GROUP BY lvl ORDER BY cnt DESC",
            params or None,
        )

        err_params = [tenant] if tenant and tenant != "all" else []
        err_where = "AND tenant=?" if tenant and tenant != "all" else ""
        top_errors = db.fetch_all(
            cur,
            f"SELECT iflow, COUNT(*) as cnt FROM logs "
            f"WHERE upper(level)='ERROR' {err_where} "
            f"GROUP BY iflow ORDER BY cnt DESC, iflow ASC LIMIT 15",
            err_params or None,
        )

        iflow_stats = db.fetch_all(
            cur,
            f"SELECT iflow, "
            f"SUM(CASE WHEN upper(level)='ERROR' THEN 1 ELSE 0 END) as error, "
            f"SUM(CASE WHEN upper(level)='WARN' THEN 1 ELSE 0 END) as warn, "
            f"SUM(CASE WHEN upper(level)='INFO' THEN 1 ELSE 0 END) as info, "
            f"COUNT(*) as total "
            f"FROM logs {where} "
            f"{'AND' if where else 'WHERE'} iflow IS NOT NULL "
            f"GROUP BY iflow "
            f"HAVING SUM(CASE WHEN upper(level)='ERROR' THEN 1 ELSE 0 END) > 0 "
            f"ORDER BY error DESC, iflow ASC",
            params or None,
        )

        timeline = db.fetch_all(
            cur,
            f"SELECT strftime(timestamp, '%Y-%m-%d %H') as hour, COUNT(*) as cnt "
            f"FROM logs {where} "
            f"{'AND' if where else 'WHERE'} upper(level)='ERROR' "
            f"AND timestamp >= (CURRENT_TIMESTAMP - INTERVAL '48 hours') "
            f"GROUP BY hour ORDER BY hour",
            params or None,
        )

        total = db.fetch_val(cur, f"SELECT COUNT(*) as cnt FROM logs {where}", params or None)

        iflow_error_count = db.fetch_val(
            cur,
            f"SELECT COUNT(DISTINCT iflow) FROM logs WHERE upper(level)='ERROR' {err_where} AND iflow IS NOT NULL",
            err_params or None,
        )

        tenant_count = db.fetch_val(
            cur,
            f"SELECT COUNT(DISTINCT tenant) FROM logs {where}",
            params or None,
        )

        per_tenant = db.fetch_all(
            cur,
            f"SELECT tenant, log_type, COUNT(*) as cnt, MAX(timestamp) as last_ts "
            f"FROM logs {where} GROUP BY tenant, log_type ORDER BY tenant",
            params or None,
        )

        return {
            "total": total or 0,
            "levels": levels,
            "top_errors": top_errors,
            "iflow_stats": iflow_stats,
            "iflow_error_count": iflow_error_count or 0,
            "tenant_count": tenant_count or 0,
            "timeline": timeline,
            "per_tenant": per_tenant,
        }

    return await db.read(_run)


# ── DB Info ───────────────────────────────────────────────────────────────────


async def get_db_info(db: Database, db_path: Path) -> dict:
    def _run(cur):
        size_bytes = db_path.stat().st_size if db_path.exists() else 0
        entries = db.fetch_val(cur, "SELECT COUNT(*) FROM logs") or 0
        tenant_count = db.fetch_val(cur, "SELECT COUNT(*) FROM tenants") or 0
        schema_version = db.fetch_val(cur, "SELECT max(version) FROM schema_version")
        return {
            "path": str(db_path),
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / 1024 / 1024, 1),
            "entries": entries,
            "tenants": tenant_count,
            "schema_version": schema_version,
        }

    return await db.read(_run)


async def clear_db(db: Database):
    def _run(conn):
        db.execute(conn, "DELETE FROM logs")
        db.execute(conn, "DELETE FROM fetch_runs")
        db.execute(conn, "DELETE FROM file_imports")
        db.execute(conn, "DELETE FROM unparsed_lines")
        # CHECKPOINT frees the blocks of the deleted rows for reuse; the file
        # itself does not shrink.
        conn.execute("CHECKPOINT")

    await db.run(_run)


async def cleanup_old_logs(db: Database, older_than_days: int, tenant: str | None = None) -> dict:
    def _run(conn):
        conditions = [f"timestamp < (CURRENT_TIMESTAMP - INTERVAL '{older_than_days} days')"]
        params = []
        if tenant and tenant != "all":
            conditions.append("tenant = ?")
            params.append(tenant)
        where = "WHERE " + " AND ".join(conditions)
        to_delete = db.fetch_val(conn, f"SELECT COUNT(*) FROM logs {where}", params or None) or 0
        db.execute(conn, f"DELETE FROM logs {where}", params or None)
        remaining = db.fetch_val(conn, "SELECT COUNT(*) FROM logs") or 0
        conn.execute("CHECKPOINT")  # reclaim disk space freed by the delete
        return {"deleted": to_delete, "remaining": remaining}

    return await db.run(_run)
