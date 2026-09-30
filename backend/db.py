"""Database layer — DuckDB."""

import asyncio
import gzip
import logging
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import pyarrow as pa

from config import get_settings

log = logging.getLogger("cpi.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    api_url       TEXT NOT NULL,
    oauth_url     TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS logs_id_seq;

CREATE TABLE IF NOT EXISTS logs (
    id          BIGINT PRIMARY KEY DEFAULT nextval('logs_id_seq'),
    tenant      TEXT NOT NULL,
    log_type    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    timestamp   TIMESTAMP NOT NULL,
    level       TEXT,
    logger      TEXT,
    iflow       TEXT,
    message     TEXT,
    ip          TEXT,
    node        TEXT,
    raw_line    TEXT,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- NOTE: no secondary indexes and no UNIQUE constraint on `logs`.
-- Benchmarked: secondary indexes give negligible read speedup (<3% on a
-- 1M-row filter+sort query) while adding real per-row insert/maintenance
-- overhead that grows with table size. The UNIQUE constraint that used to
-- sit here (tenant, log_type, filename, timestamp, level, logger, message)
-- has the *same* problem but far worse: DuckDB maintains an ART index to
-- enforce it, and per-row conflict checking against that index gets
-- progressively slower as the table grows — measured degrading from ~9.5 to
-- ~3 files/hour (100% CPU) once `logs` passed ~5.8M rows. Duplicate
-- protection across fetch runs is instead provided entirely by
-- `file_imports.lines` (only rows beyond the last-imported line are ever
-- parsed/inserted into `import_log_file()`) — see migration below for how this
-- constraint is retroactively dropped from existing DB files.
DROP INDEX IF EXISTS idx_logs_tenant;
DROP INDEX IF EXISTS idx_logs_level;
DROP INDEX IF EXISTS idx_logs_iflow;
DROP INDEX IF EXISTS idx_logs_timestamp;
DROP INDEX IF EXISTS idx_logs_tenant_ts;
DROP INDEX IF EXISTS idx_logs_level_ts;
DROP INDEX IF EXISTS idx_logs_tenant_level;

CREATE TABLE IF NOT EXISTS file_imports (
    tenant   TEXT NOT NULL,
    filename TEXT NOT NULL,
    lines    INTEGER NOT NULL DEFAULT 0,
    size     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, filename)
);

CREATE SEQUENCE IF NOT EXISTS fetch_runs_id_seq;

CREATE TABLE IF NOT EXISTS fetch_runs (
    id               BIGINT PRIMARY KEY DEFAULT nextval('fetch_runs_id_seq'),
    tenant           TEXT,
    log_type         TEXT,
    started_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at      TEXT,
    files_total      INTEGER DEFAULT 0,
    files_done       INTEGER DEFAULT 0,
    entries_imported INTEGER DEFAULT 0,
    status           TEXT DEFAULT 'running'
);

-- Recurring fetch configurations (e.g. "pull tenant X every 15 min for the
-- last 1h"). Checked periodically by the background scheduler loop in
-- main.py. tenants/log_types are stored as JSON arrays (or '["all"]').
CREATE TABLE IF NOT EXISTS fetch_schedules (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    tenants           TEXT NOT NULL,   -- JSON array of tenant ids, or '["all"]'
    log_types         TEXT NOT NULL,   -- JSON array, e.g. '["trace","http"]'
    hours             INTEGER NOT NULL DEFAULT 24,
    interval_minutes  INTEGER NOT NULL DEFAULT 15,
    enabled           BOOLEAN NOT NULL DEFAULT true,
    last_run_at       TEXT,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Generic key/value app settings (e.g. the last-used fetch form config,
-- saved as a default so the UI doesn't always start from "all tenants").
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS unparsed_lines_id_seq;

-- Lines that iter_log_batches() could neither match against LINE_RE nor
-- attach as a continuation of the previous parsed row (i.e. the very first
-- line of a file/parse run is itself unparsable, so there is no prior
-- message to append it to). Kept here instead of being silently dropped so
-- log imports stay recoverable/auditable even for unexpected line formats.
CREATE TABLE IF NOT EXISTS unparsed_lines (
    id          BIGINT PRIMARY KEY DEFAULT nextval('unparsed_lines_id_seq'),
    tenant      TEXT NOT NULL,
    log_type    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    raw_text    TEXT,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


READ_POOL_SIZE = 4

# Dedicated, bounded threads for DB work instead of asyncio's shared default
# executor: reads can never occupy more threads than there are cursors, and
# the single writer always has its own thread.
_read_executor = ThreadPoolExecutor(READ_POOL_SIZE, thread_name_prefix="duckdb-read")
_write_executor = ThreadPoolExecutor(1, thread_name_prefix="duckdb-write")


class DBBusyError(Exception):
    """The database could not serve the request in time (mapped to HTTP 503)."""


def _duckdb_config() -> dict:
    # DuckDB defaults to 80% of the RAM it can see — inside Docker that is the
    # whole VM, not the container limit — and to one thread per CPU. Together
    # with the Python heap this pushed the process past the VM memory and got
    # it OOM-killed. Keep the engine inside an explicit budget instead.
    settings = get_settings()
    config: dict[str, str | int] = {
        "memory_limit": settings.duckdb_memory_limit,
        "threads": settings.duckdb_threads,
        "checkpoint_threshold": settings.duckdb_checkpoint_threshold,
    }
    if settings.duckdb_temp_dir:
        config["temp_directory"] = settings.duckdb_temp_dir
    return config


def _consume_result(fut: asyncio.Future) -> None:
    """Mark the outcome of a shielded DB call as retrieved. When the caller timed
    out or was cancelled, nobody awaits the future any more, and asyncio would log
    the (expected) InterruptException as "Future exception was never retrieved"."""
    if not fut.cancelled():
        fut.exception()


class DuckDBConnection:
    """Singleton async wrapper around a synchronous DuckDB connection.

    Writes (INSERT/UPDATE/DELETE) go through the single main connection,
    serialized by `_write_lock` — DuckDB only allows one writer at a time.

    Reads (SELECT) go through a small pool of `cursor()` objects derived from
    that same connection. DuckDB cursors share the underlying database handle
    and can run concurrently with an in-progress write from another cursor
    (MVCC snapshot reads) — this is what keeps the UI (tenants, stats, log
    browsing) responsive while a large fetch/import job is writing in the
    background, instead of queuing behind it.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        """Wrap an open connection whose schema is up to date; see open()."""
        self._conn = conn
        self._write_lock = asyncio.Lock()
        self._read_pool: asyncio.Queue = asyncio.Queue()
        for _ in range(READ_POOL_SIZE):
            self._read_pool.put_nowait(self._conn.cursor())

    @classmethod
    def open(cls, path: Path) -> "DuckDBConnection":
        """Connect with the configured memory budget and bring the schema up to date."""
        conn = duckdb.connect(str(path), config=_duckdb_config())
        try:
            memory_limit, threads = conn.execute(
                "SELECT current_setting('memory_limit'), current_setting('threads')"
            ).fetchall()[0]
            log.info(f"duckdb {duckdb.__version__}: memory_limit={memory_limit}, threads={threads}")
            _create_schema(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    @staticmethod
    def _execute(cur, sql: str, params=None):
        if params:
            return cur.execute(sql, params)
        return cur.execute(sql)

    @classmethod
    def _fetchall_dicts(cls, cur, sql: str, params=None) -> list[dict]:
        c = cls._execute(cur, sql, params)
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row, strict=True)) for row in c.fetchall()]

    @classmethod
    def _fetchone_dict(cls, cur, sql: str, params=None) -> dict | None:
        c = cls._execute(cur, sql, params)
        if c.description is None:
            return None
        cols = [d[0] for d in c.description]
        # fetchall(), not fetchone(): a partially consumed result keeps the
        # cursor's query open, and in DuckDB < 1.5 that blocks every automatic
        # CHECKPOINT — the writer (and with it the fetch job) then hangs until
        # the cursor happens to be reused. All callers select at most one row.
        rows = c.fetchall()
        return dict(zip(cols, rows[0], strict=True)) if rows else None

    @classmethod
    def _fetchone_val(cls, cur, sql: str, params=None):
        c = cls._execute(cur, sql, params)
        rows = c.fetchall()  # see _fetchone_dict
        return rows[0][0] if rows else None

    async def run(self, fn, *args, **kwargs):
        """Write path: run a synchronous DB function against the main connection,
        serialized by `_write_lock`, off the event loop (in a worker thread) so a
        long-running write never blocks other requests from being scheduled.

        The lock is released only once the in-flight call has actually finished
        (via a done-callback), even if the awaiting request is cancelled early —
        otherwise a new writer could start against the same connection while the
        abandoned one is still running, corrupting/blocking future calls."""
        try:
            # How long a write waits for the single writer before giving up (HTTP 503).
            await asyncio.wait_for(self._write_lock.acquire(), get_settings().write_lock_timeout_s)
        except TimeoutError:
            log.warning(f"gave up waiting {get_settings().write_lock_timeout_s:.0f}s for the write lock")
            raise DBBusyError("database busy: another write is still running") from None
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_write_executor, lambda: fn(self._conn, *args, **kwargs))

        def release(f: asyncio.Future) -> None:
            _consume_result(f)
            self._write_lock.release()

        fut.add_done_callback(release)
        return await asyncio.shield(fut)

    async def read(self, fn, *args, **kwargs):
        """Read path: borrow a cursor from the pool and run a synchronous SELECT
        against it, off the event loop. Runs concurrently with `run()` writes and
        with other `read()` calls (bounded by READ_POOL_SIZE).

        Backpressure and timeouts, so slow or abandoned queries can't lock up
        the API for minutes:
        - waiting for a free cursor is bounded (POOL_ACQUIRE_TIMEOUT_S → DBBusyError, HTTP 503);
        - a query running longer than QUERY_TIMEOUT_S, or whose caller was
          cancelled, is stopped with `cursor.interrupt()`.

        `asyncio.shield` + a done-callback still make sure the cursor only
        returns to the pool once its query has actually finished, so a new
        reader never gets a cursor that is still executing."""
        try:
            cur = await asyncio.wait_for(self._read_pool.get(), get_settings().pool_acquire_timeout_s)
        except TimeoutError:
            raise DBBusyError("database busy: no free read connection") from None
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_read_executor, lambda: fn(cur, *args, **kwargs))

        def give_back(f: asyncio.Future) -> None:
            _consume_result(f)
            self._read_pool.put_nowait(cur)

        fut.add_done_callback(give_back)
        t0 = time.perf_counter()
        try:
            return await asyncio.wait_for(asyncio.shield(fut), get_settings().query_timeout_s)
        except TimeoutError:
            cur.interrupt()
            log.warning(f"query interrupted after {time.perf_counter() - t0:.1f}s (timeout)")
            raise DBBusyError(f"query took longer than {get_settings().query_timeout_s:g}s and was cancelled") from None
        except asyncio.CancelledError:
            cur.interrupt()
            raise


# ── Singleton connection ──────────────────────────────────────────────────────
_db_instance: DuckDBConnection | None = None


def _migrate_drop_logs_unique_constraint(conn: duckdb.DuckDBPyConnection):
    """One-off migration for DBs created before the UNIQUE constraint on `logs`
    was removed (see comment in SCHEMA). `CREATE TABLE IF NOT EXISTS` never
    retroactively changes an existing table's constraints, so DBs created by an
    older version of this app keep the old, insert-performance-killing UNIQUE
    constraint forever unless we explicitly rebuild the table here. Idempotent:
    does nothing once the constraint is gone."""
    has_unique = conn.execute("""
        SELECT 1 FROM duckdb_constraints()
        WHERE table_name = 'logs' AND constraint_type = 'UNIQUE'
        LIMIT 1
    """).fetchone()
    if not has_unique:
        return

    log.info("migrate: Dropping legacy UNIQUE constraint on logs (rebuilding table)...")
    conn.execute("""
        CREATE TABLE logs_new (
            id          BIGINT PRIMARY KEY DEFAULT nextval('logs_id_seq'),
            tenant      TEXT NOT NULL,
            log_type    TEXT NOT NULL,
            filename    TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            level       TEXT,
            logger      TEXT,
            iflow       TEXT,
            message     TEXT,
            ip          TEXT,
            node        TEXT,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        INSERT INTO logs_new (id, tenant, log_type, filename, timestamp, level,
                               logger, iflow, message, ip, node, imported_at)
        SELECT id, tenant, log_type, filename, timestamp, level,
               logger, iflow, message, ip, node, imported_at
        FROM logs
    """)
    conn.execute("DROP TABLE logs")
    conn.execute("ALTER TABLE logs_new RENAME TO logs")
    # Keep the sequence ahead of the max copied id so future inserts don't collide.
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchall()[0][0]
    conn.execute(f"ALTER SEQUENCE logs_id_seq RESTART WITH {max_id + 1}")
    conn.execute("CHECKPOINT")
    log.info("migrate: Done — UNIQUE constraint removed from logs.")


def _migrate_timestamp_to_native(conn: duckdb.DuckDBPyConnection):
    """One-off migration for DBs created before `timestamp` was a native
    TIMESTAMP column (older versions stored it as TEXT). Native TIMESTAMP
    enables DuckDB zonemap pruning on time-range filters (used everywhere:
    query_logs, get_stats, cleanup_old_logs) and stores more compactly than
    the equivalent TEXT. Idempotent: no-op once the column is already
    TIMESTAMP. Safe to run on any size table — all values in `logs.timestamp`
    are written by iter_log_batches() in the strict 'YYYY-MM-DD HH:MM:SS'
    format (see LINE_RE), which DuckDB parses unambiguously."""
    col_type = conn.execute("""
        SELECT data_type FROM duckdb_columns()
        WHERE table_name = 'logs' AND column_name = 'timestamp'
    """).fetchone()
    if not col_type or col_type[0].upper() == "TIMESTAMP":
        return

    log.info("migrate: Converting logs.timestamp from TEXT to native TIMESTAMP...")
    conn.execute("ALTER TABLE logs ALTER COLUMN timestamp TYPE TIMESTAMP USING CAST(timestamp AS TIMESTAMP)")
    conn.execute("CHECKPOINT")
    log.info("migrate: Done — logs.timestamp is now native TIMESTAMP.")


def _migrate_add_raw_line_column(conn: duckdb.DuckDBPyConnection):
    """One-off migration for DBs created before `logs.raw_line` existed.
    `CREATE TABLE IF NOT EXISTS` never adds columns to an already-existing
    table, so this ALTER is needed for any DB file created by an older
    version of this app. Existing rows get raw_line=NULL (no backfill from
    the original log files — only newly imported rows get it populated).
    Idempotent: no-op once the column already exists."""
    has_col = conn.execute("""
        SELECT 1 FROM duckdb_columns()
        WHERE table_name = 'logs' AND column_name = 'raw_line'
    """).fetchone()
    if has_col:
        return

    log.info("migrate: Adding logs.raw_line column...")
    conn.execute("ALTER TABLE logs ADD COLUMN raw_line TEXT")
    conn.execute("CHECKPOINT")
    log.info("migrate: Done — logs.raw_line added (NULL for pre-existing rows).")


def _create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    _migrate_drop_logs_unique_constraint(conn)
    _migrate_timestamp_to_native(conn)
    _migrate_add_raw_line_column(conn)


async def init_db(path: Path):
    global _db_instance
    _db_instance = DuckDBConnection.open(path)


async def get_db() -> DuckDBConnection:
    if _db_instance is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _db_instance


LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})#[^#]*#([^#]*)#([^#]*)#[^#]*#([^#]*)#[^#]*"
    r"#[^#]*#[^#]*#[^#]*#[^#]*#(.*?)#-#([^#]*)#([^\n]*)$"
)

_IFLOW_RE = re.compile(
    r"Camel \(([^)]+)\)"
    r"|(?:scheduler-|\d+-)"
    r"(.+?)(?:_Worker.*)?$"
)


def _extract_iflow(thread: str) -> str:
    thread = thread.strip()
    m = _IFLOW_RE.match(thread)
    if m:
        return (m.group(1) or m.group(2) or thread).strip()
    return thread


def _is_gzip(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except Exception:
        return False


def iter_log_batches(
    tenant: str, log_type: str, filepath: Path, batch_size: int = 20000
) -> Iterator[tuple[list[tuple], list[tuple]]]:
    """Parse a CPI log file into structured rows, in batches of `batch_size`.

    Yields (rows, unparsed):
    - rows: tuples matching _INSERT_COLUMNS order (tenant, log_type,
      filename, timestamp, level, logger, iflow, message, ip, node, raw_line).
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
    batch: list[tuple] = []
    unparsed: list[tuple] = []
    current: list | None = None
    opener = gzip.open if _is_gzip(filepath) else open
    with opener(filepath, "rt", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            raw = line.rstrip("\n")
            m = LINE_RE.match(raw)
            if not m:
                if current is not None:
                    # Continuation line (e.g. stacktrace) — append to the
                    # current row's message and raw_line.
                    current[7] += "\n" + raw
                    current[10] += "\n" + raw
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
                _extract_iflow(iflow),
                message.strip(),
                ip.strip(),
                node.strip(),
                raw,
            ]
    if current is not None:
        batch.append(tuple(current))
    if batch or unparsed:
        yield batch, unparsed


# ── Tenants ───────────────────────────────────────────────────────────────────


async def upsert_tenant(
    db: DuckDBConnection, tenant_id: str, name: str, api_url: str, oauth_url: str, client_id: str, client_secret: str
):
    def _run(conn):
        db._execute(
            conn,
            """
            INSERT INTO tenants (id, name, api_url, oauth_url, client_id, client_secret)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name=excluded.name, api_url=excluded.api_url,
                oauth_url=excluded.oauth_url, client_id=excluded.client_id,
                client_secret=excluded.client_secret
        """,
            [tenant_id, name, api_url, oauth_url, client_id, client_secret],
        )

    await db.run(_run)


async def get_tenants(db: DuckDBConnection) -> list[dict]:
    return await db.read(db._fetchall_dicts, "SELECT * FROM tenants ORDER BY id")


async def get_tenant(db: DuckDBConnection, tenant_id: str) -> dict | None:
    return await db.read(db._fetchone_dict, "SELECT * FROM tenants WHERE id=?", [tenant_id])


async def delete_tenant(db: DuckDBConnection, tenant_id: str):
    await db.run(db._execute, "DELETE FROM tenants WHERE id=?", [tenant_id])


# ── Fetch Schedules ───────────────────────────────────────────────────────────
async def create_schedule(
    db: DuckDBConnection,
    schedule_id: str,
    name: str,
    tenants_json: str,
    log_types_json: str,
    hours: int,
    interval_minutes: int,
    enabled: bool,
):
    def _run(conn):
        db._execute(
            conn,
            """
            INSERT INTO fetch_schedules (id, name, tenants, log_types, hours, interval_minutes, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            [schedule_id, name, tenants_json, log_types_json, hours, interval_minutes, enabled],
        )

    await db.run(_run)


async def update_schedule(
    db: DuckDBConnection,
    schedule_id: str,
    name: str,
    tenants_json: str,
    log_types_json: str,
    hours: int,
    interval_minutes: int,
    enabled: bool,
):
    def _run(conn):
        db._execute(
            conn,
            """
            UPDATE fetch_schedules
            SET name=?, tenants=?, log_types=?, hours=?, interval_minutes=?, enabled=?
            WHERE id=?
        """,
            [name, tenants_json, log_types_json, hours, interval_minutes, enabled, schedule_id],
        )

    await db.run(_run)


async def get_schedules(db: DuckDBConnection) -> list[dict]:
    return await db.read(db._fetchall_dicts, "SELECT * FROM fetch_schedules ORDER BY created_at")


async def get_schedule(db: DuckDBConnection, schedule_id: str) -> dict | None:
    return await db.read(db._fetchone_dict, "SELECT * FROM fetch_schedules WHERE id=?", [schedule_id])


async def delete_schedule(db: DuckDBConnection, schedule_id: str):
    await db.run(db._execute, "DELETE FROM fetch_schedules WHERE id=?", [schedule_id])


async def touch_schedule_last_run(db: DuckDBConnection, schedule_id: str):
    await db.run(db._execute, "UPDATE fetch_schedules SET last_run_at=CURRENT_TIMESTAMP WHERE id=?", [schedule_id])


# ── Settings (key/value) ──────────────────────────────────────────────────────
async def get_setting(db: DuckDBConnection, key: str) -> str | None:
    row = await db.read(db._fetchone_dict, "SELECT value FROM settings WHERE key=?", [key])
    return row["value"] if row else None


async def set_setting(db: DuckDBConnection, key: str, value: str):
    def _run(conn):
        db._execute(
            conn,
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value=excluded.value
        """,
            [key, value],
        )

    await db.run(_run)


# ── Log Import ────────────────────────────────────────────────────────────────


async def update_file_import_size(db: DuckDBConnection, tenant: str, filename: str, size: int):
    await db.run(
        db._execute,
        "UPDATE file_imports SET size=? WHERE tenant=? AND filename=?",
        [size, tenant, filename],
    )


async def get_file_import(db: DuckDBConnection, tenant: str, filename: str) -> dict:
    # Fetch-job bookkeeping goes through the writer (which the job needs
    # anyway) so a read pool saturated by the UI can never fail a running job.
    row = await db.run(
        db._fetchone_dict,
        "SELECT lines, size FROM file_imports WHERE tenant=? AND filename=?",
        [tenant, filename],
    )
    return row if row else {"lines": 0, "size": 0}


# Row count per parsed/inserted batch. Batching keeps peak memory bounded
# for very large files; pyarrow avoids the per-row Python->DuckDB
# parameter-marshalling cost entirely (see _insert_rows).
_INSERT_BATCH_SIZE = 20000
_INSERT_COLUMNS = [
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
]


def _insert_rows(conn, rows: list[tuple]):
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
        {col: pa.array(values, type=pa.string()) for col, values in zip(_INSERT_COLUMNS, columns, strict=True)}
    )
    conn.register("_import_batch", arrow_table)
    try:
        conn.execute(
            f"INSERT INTO logs ({','.join(_INSERT_COLUMNS)}) SELECT {','.join(_INSERT_COLUMNS)} FROM _import_batch"
        )
    finally:
        conn.unregister("_import_batch")


async def import_log_file(
    db: DuckDBConnection, tenant: str, log_type: str, filepath: Path, filename: str, already: int, size: int = 0
) -> int:
    """Parse `filepath` and import every row past the first `already` rows.
    Returns the number of newly inserted rows.

    Parsing and inserting both run batch by batch in the single DB writer
    thread: the event loop never runs CPU-bound parsing, and memory is bounded
    by one batch instead of the whole file.

    Duplicate protection: there is no UNIQUE constraint / ON CONFLICT /
    dedupe on `logs` (see SCHEMA comment). Protection across fetch runs comes
    entirely from file_imports.lines — only rows past the last-imported row
    of a file are inserted. Legitimate repeated log lines (e.g. heartbeats
    with identical timestamp/level/logger/message) are distinct entries and
    are imported as such.

    Each batch is inserted in one transaction together with the updated
    file_imports.lines, so the offset always matches what is committed: if
    the import stops midway, the next fetch continues where it stopped
    instead of duplicating rows. file_imports.size (which lets the next
    fetch skip an unchanged file) is only written once the whole file was
    parsed. Unparsable lines before the first row are stored only on the
    first import of a file, not again on every re-fetch."""

    def _run(conn):
        total = inserted = 0
        for rows, unparsed in iter_log_batches(tenant, log_type, filepath, _INSERT_BATCH_SIZE):
            skip = max(0, already - total)
            total += len(rows)
            new = rows[skip:]
            if not new and not (unparsed and already == 0):
                continue
            conn.begin()
            try:
                if new:
                    _insert_rows(conn, new)
                    db._execute(
                        conn,
                        """
                        INSERT INTO file_imports (tenant, filename, lines, size) VALUES (?, ?, ?, 0)
                        ON CONFLICT (tenant, filename) DO UPDATE SET lines=excluded.lines
                    """,
                        [tenant, filename, total],
                    )
                if unparsed and already == 0:
                    conn.executemany(
                        """
                        INSERT INTO unparsed_lines (tenant, log_type, filename, line_no, raw_text)
                        VALUES (?, ?, ?, ?, ?)
                    """,
                        [[tenant, log_type, filename, n, t] for n, t in unparsed],
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            inserted += len(new)
            invalidate_stats_cache()
        if total:
            db._execute(
                conn,
                """
                INSERT INTO file_imports (tenant, filename, lines, size) VALUES (?, ?, ?, ?)
                ON CONFLICT (tenant, filename) DO UPDATE SET lines=excluded.lines, size=excluded.size
            """,
                [tenant, filename, max(total, already), size],
            )
        return inserted

    return await db.run(_run)


# ── Query ─────────────────────────────────────────────────────────────────────

# Columns of the log list. raw_line (the full original line, often as large
# as the message again) is left out: it is only shown in the detail view,
# which loads it via get_log_entry(). Reading it for every listed row
# roughly doubled the data each list query had to scan and transfer.
_LIST_COLUMNS = "id, tenant, log_type, filename, timestamp, level, logger, iflow, message, ip, node, imported_at"


async def query_logs(
    db: DuckDBConnection,
    *,
    tenant: str | None = None,
    level: str | None = None,
    iflow: str | None = None,
    grep: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    def _run(cur):
        conditions, params = [], []

        if tenant and tenant != "all":
            conditions.append("tenant = ?")
            params.append(tenant)
        if level and level != "ALL":
            conditions.append("upper(level) = ?")
            params.append(level.upper())
        if iflow:
            conditions.append("iflow LIKE ?")
            params.append(f"%{iflow}%")
        if grep:
            conditions.append("(message LIKE ? OR logger LIKE ?)")
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

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        total = db._fetchone_val(cur, f"SELECT COUNT(*) FROM logs {where}", params or None)

        offset = (page - 1) * page_size
        items = db._fetchall_dicts(
            cur,
            f"SELECT {_LIST_COLUMNS} FROM logs {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        )

        return {
            "total": total or 0,
            "page": page,
            "page_size": page_size,
            "pages": max(1, ((total or 0) + page_size - 1) // page_size),
            "items": items,
        }

    return await db.read(_run)


async def get_log_entry(db: DuckDBConnection, entry_id: int) -> dict | None:
    return await db.read(db._fetchone_dict, "SELECT * FROM logs WHERE id=?", [entry_id])


# ── Stats ─────────────────────────────────────────────────────────────────────

# get_stats() runs five aggregations over the whole table; the Stats page and
# its auto-refresh called it on every visit. Results are cached briefly and
# dropped whenever imports, cleanup or clear change the data.
_stats_cache: dict[str, tuple[float, dict]] = {}
_stats_locks: dict[str, asyncio.Lock] = {}


def invalidate_stats_cache() -> None:
    _stats_cache.clear()


async def get_stats(db: DuckDBConnection, tenant: str | None = None) -> dict:
    key = tenant or "all"
    cached = _stats_cache.get(key)
    if cached and time.monotonic() - cached[0] < get_settings().stats_cache_seconds:
        return cached[1]
    # Single flight: concurrent requests for the same key share one computation.
    async with _stats_locks.setdefault(key, asyncio.Lock()):
        cached = _stats_cache.get(key)
        if cached and time.monotonic() - cached[0] < get_settings().stats_cache_seconds:
            return cached[1]
        result = await _compute_stats(db, tenant)
        _stats_cache[key] = (time.monotonic(), result)
        return result


async def _compute_stats(db: DuckDBConnection, tenant: str | None = None) -> dict:
    def _run(cur):
        where = "WHERE tenant=?" if tenant and tenant != "all" else ""
        params = [tenant] if tenant and tenant != "all" else []

        levels = db._fetchall_dicts(
            cur,
            f"SELECT upper(level) as lvl, COUNT(*) as cnt FROM logs {where} GROUP BY lvl ORDER BY cnt DESC",
            params or None,
        )

        err_params = [tenant] if tenant and tenant != "all" else []
        err_where = "AND tenant=?" if tenant and tenant != "all" else ""
        top_errors = db._fetchall_dicts(
            cur,
            f"SELECT iflow, COUNT(*) as cnt FROM logs "
            f"WHERE upper(level)='ERROR' {err_where} "
            f"GROUP BY iflow ORDER BY cnt DESC LIMIT 15",
            err_params or None,
        )

        timeline = db._fetchall_dicts(
            cur,
            f"SELECT strftime(timestamp, '%Y-%m-%d %H') as hour, COUNT(*) as cnt "
            f"FROM logs {where} "
            f"{'AND' if where else 'WHERE'} upper(level)='ERROR' "
            f"AND timestamp >= (CURRENT_TIMESTAMP - INTERVAL '48 hours') "
            f"GROUP BY hour ORDER BY hour",
            params or None,
        )

        total = db._fetchone_val(cur, f"SELECT COUNT(*) as cnt FROM logs {where}", params or None)

        per_tenant = db._fetchall_dicts(
            cur,
            f"SELECT tenant, log_type, COUNT(*) as cnt, MAX(timestamp) as last_ts "
            f"FROM logs {where} GROUP BY tenant, log_type ORDER BY tenant",
            params or None,
        )

        return {
            "total": total or 0,
            "levels": levels,
            "top_errors": top_errors,
            "timeline": timeline,
            "per_tenant": per_tenant,
        }

    return await db.read(_run)


# ── DB Info ───────────────────────────────────────────────────────────────────


async def get_db_info(db: DuckDBConnection, db_path: Path) -> dict:
    def _run(cur):
        size_bytes = db_path.stat().st_size if db_path.exists() else 0
        entries = db._fetchone_val(cur, "SELECT COUNT(*) FROM logs") or 0
        tenant_count = db._fetchone_val(cur, "SELECT COUNT(*) FROM tenants") or 0
        return {
            "path": str(db_path),
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / 1024 / 1024, 1),
            "entries": entries,
            "tenants": tenant_count,
        }

    return await db.read(_run)


async def clear_db(db: DuckDBConnection):
    def _run(conn):
        db._execute(conn, "DELETE FROM logs")
        db._execute(conn, "DELETE FROM fetch_runs")
        db._execute(conn, "DELETE FROM file_imports")
        # DuckDB doesn't reclaim freed disk space from deletes automatically;
        # CHECKPOINT forces a rewrite of the underlying row groups so the
        # .duckdb file actually shrinks back down instead of permanently
        # keeping the pre-delete size.
        conn.execute("CHECKPOINT")

    await db.run(_run)
    invalidate_stats_cache()


async def cleanup_old_logs(db: DuckDBConnection, older_than_days: int, tenant: str | None = None) -> dict:
    def _run(conn):
        conditions = [f"timestamp < (CURRENT_TIMESTAMP - INTERVAL '{older_than_days} days')"]
        params = []
        if tenant and tenant != "all":
            conditions.append("tenant = ?")
            params.append(tenant)
        where = "WHERE " + " AND ".join(conditions)
        to_delete = db._fetchone_val(conn, f"SELECT COUNT(*) FROM logs {where}", params or None) or 0
        db._execute(conn, f"DELETE FROM logs {where}", params or None)
        remaining = db._fetchone_val(conn, "SELECT COUNT(*) FROM logs") or 0
        conn.execute("CHECKPOINT")  # reclaim disk space freed by the delete
        return {"deleted": to_delete, "remaining": remaining}

    result = await db.run(_run)
    invalidate_stats_cache()
    return result
