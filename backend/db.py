"""Database layer — DuckDB."""
import re
import os
import gzip
import asyncio
import duckdb
import pyarrow as pa
from pathlib import Path
from typing import Optional

DB_PATH: Path = Path("/data/cpi_logs.db")

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
-- parsed/inserted into `import_rows()`) — see migration below for how this
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

-- Lines that parse_log_file() could neither match against LINE_RE nor
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
# How long a write waits for the single writer before giving up (HTTP 503).
WRITE_LOCK_TIMEOUT_S = float(os.getenv("WRITE_LOCK_TIMEOUT_S", "120"))


class DBBusyError(Exception):
    """The database could not serve the request in time (mapped to HTTP 503)."""

# DuckDB defaults to 80% of the RAM it can see — inside Docker that is the
# whole VM, not the container limit — and to one thread per CPU. Together with
# the Python heap this pushed the process past the VM memory and got it
# OOM-killed. Keep the engine inside an explicit budget instead.
DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "1.5GB")
DUCKDB_THREADS      = int(os.getenv("DUCKDB_THREADS", "4"))
DUCKDB_TEMP_DIR     = os.getenv("DUCKDB_TEMP_DIR", "")  # "" = DuckDB default (<db file>.tmp)


def _duckdb_config() -> dict:
    config = {"memory_limit": DUCKDB_MEMORY_LIMIT, "threads": DUCKDB_THREADS}
    if DUCKDB_TEMP_DIR:
        config["temp_directory"] = DUCKDB_TEMP_DIR
    return config


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

    def __init__(self, path: Path):
        self._conn = duckdb.connect(str(path), config=_duckdb_config())
        self._write_lock = asyncio.Lock()
        self._read_pool: asyncio.Queue = asyncio.Queue()
        for _ in range(READ_POOL_SIZE):
            self._read_pool.put_nowait(self._conn.cursor())

    @staticmethod
    def _execute(cur, sql: str, params=None):
        if params:
            return cur.execute(sql, params)
        return cur.execute(sql)

    @classmethod
    def _fetchall_dicts(cls, cur, sql: str, params=None) -> list[dict]:
        c = cls._execute(cur, sql, params)
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in c.fetchall()]

    @classmethod
    def _fetchone_dict(cls, cur, sql: str, params=None) -> Optional[dict]:
        c = cls._execute(cur, sql, params)
        if c.description is None:
            return None
        cols = [d[0] for d in c.description]
        # fetchall(), not fetchone(): a partially consumed result keeps the
        # cursor's query open, and in DuckDB < 1.5 that blocks every automatic
        # CHECKPOINT — the writer (and with it the fetch job) then hangs until
        # the cursor happens to be reused. All callers select at most one row.
        rows = c.fetchall()
        return dict(zip(cols, rows[0])) if rows else None

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
            await asyncio.wait_for(self._write_lock.acquire(), WRITE_LOCK_TIMEOUT_S)
        except asyncio.TimeoutError:
            print(f"[db] gave up waiting {WRITE_LOCK_TIMEOUT_S:.0f}s for the write lock")
            raise DBBusyError("database busy: another write is still running") from None
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, lambda: fn(self._conn, *args, **kwargs))
        fut.add_done_callback(lambda f: self._write_lock.release())
        return await asyncio.shield(fut)

    async def read(self, fn, *args, **kwargs):
        """Read path: borrow a cursor from the pool and run a synchronous SELECT
        against it, off the event loop. Runs concurrently with `run()` writes and
        with other `read()` calls (bounded by READ_POOL_SIZE).

        Uses `asyncio.shield` + a done-callback so the cursor is only returned to
        the pool once its query has actually finished, even if the caller is
        cancelled early — otherwise a cancelled request could hand the cursor to
        a new reader while the abandoned query is still executing on it."""
        cur = await self._read_pool.get()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, lambda: fn(cur, *args, **kwargs))
        fut.add_done_callback(lambda f: self._read_pool.put_nowait(cur))
        return await asyncio.shield(fut)

    async def close(self):
        pass  # singleton — stays open for the lifetime of the process


# ── Singleton connection ──────────────────────────────────────────────────────
_db_instance: Optional[DuckDBConnection] = None


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

    print("[migrate] Dropping legacy UNIQUE constraint on logs (rebuilding table)...")
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
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0]
    conn.execute(f"ALTER SEQUENCE logs_id_seq RESTART WITH {max_id + 1}")
    conn.execute("CHECKPOINT")
    print("[migrate] Done — UNIQUE constraint removed from logs.")


def _migrate_timestamp_to_native(conn: duckdb.DuckDBPyConnection):
    """One-off migration for DBs created before `timestamp` was a native
    TIMESTAMP column (older versions stored it as TEXT). Native TIMESTAMP
    enables DuckDB zonemap pruning on time-range filters (used everywhere:
    query_logs, get_stats, cleanup_old_logs) and stores more compactly than
    the equivalent TEXT. Idempotent: no-op once the column is already
    TIMESTAMP. Safe to run on any size table — all values in `logs.timestamp`
    are written by parse_log_file() in the strict 'YYYY-MM-DD HH:MM:SS'
    format (see LINE_RE), which DuckDB parses unambiguously."""
    col_type = conn.execute("""
        SELECT data_type FROM duckdb_columns()
        WHERE table_name = 'logs' AND column_name = 'timestamp'
    """).fetchone()
    if not col_type or col_type[0].upper() == "TIMESTAMP":
        return

    print("[migrate] Converting logs.timestamp from TEXT to native TIMESTAMP...")
    conn.execute(
        "ALTER TABLE logs ALTER COLUMN timestamp TYPE TIMESTAMP USING CAST(timestamp AS TIMESTAMP)"
    )
    conn.execute("CHECKPOINT")
    print("[migrate] Done — logs.timestamp is now native TIMESTAMP.")


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

    print("[migrate] Adding logs.raw_line column...")
    conn.execute("ALTER TABLE logs ADD COLUMN raw_line TEXT")
    conn.execute("CHECKPOINT")
    print("[migrate] Done — logs.raw_line added (NULL for pre-existing rows).")


async def init_db(path: Path = DB_PATH):
    global _db_instance
    conn = duckdb.connect(str(path), config=_duckdb_config())
    memory_limit, threads = conn.execute(
        "SELECT current_setting('memory_limit'), current_setting('threads')"
    ).fetchall()[0]
    print(f"[db] duckdb {duckdb.__version__}: memory_limit={memory_limit}, threads={threads}")
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    _migrate_drop_logs_unique_constraint(conn)
    _migrate_timestamp_to_native(conn)
    _migrate_add_raw_line_column(conn)
    _db_instance = DuckDBConnection.__new__(DuckDBConnection)
    _db_instance._conn = conn
    _db_instance._write_lock = asyncio.Lock()
    _db_instance._read_pool = asyncio.Queue()
    for _ in range(READ_POOL_SIZE):
        _db_instance._read_pool.put_nowait(conn.cursor())


async def get_db(path: Path = DB_PATH) -> DuckDBConnection:
    if _db_instance is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _db_instance


LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})#[^#]*#([^#]*)#([^#]*)#[^#]*#([^#]*)#[^#]*'
    r'#[^#]*#[^#]*#[^#]*#[^#]*#(.*?)#-#([^#]*)#([^\n]*)$'
)

_IFLOW_RE = re.compile(
    r'Camel \(([^)]+)\)'
    r'|(?:scheduler-|\d+-)'
    r'(.+?)(?:_Worker.*)?$'
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


def parse_log_file(tenant: str, log_type: str, filepath: Path) -> tuple[list[tuple], list[tuple]]:
    """Parse a CPI log file into structured rows.

    Returns (rows, unparsed):
    - rows: list of tuples matching _INSERT_COLUMNS order (tenant, log_type,
      filename, timestamp, level, logger, iflow, message, ip, node, raw_line).
    - unparsed: list of (line_no, raw_text) for lines that could not be
      matched against LINE_RE *and* had no preceding parsed row in this file
      to attach to (see below) — kept so nothing is silently dropped.

    Lines that don't match LINE_RE (e.g. a stacktrace continuation of a
    multi-line log message) are appended to the message/raw_line of the
    previously parsed row instead of being discarded, as long as there is a
    previous row in this file to attach them to.

    NOTE on incremental re-fetch bookkeeping (see import_rows/file_imports):
    the file's whole content is re-parsed on every fetch, and only rows past
    the offset stored in file_imports.lines are (re-)imported — that offset
    counts *top-level rows* (post-merge), not raw physical lines. This is
    safe for the normal case (a file only ever grows by new complete lines
    at the end). Edge case: if a multi-line message is only partially
    written when a fetch runs (e.g. a stacktrace still being flushed) and
    more continuation lines for that *same* message appear by the next
    fetch, those extra lines won't be picked up — the row they belong to is
    already before the offset. This is considered acceptable: rare, and the
    message is still captured (just possibly truncated to what existed at
    fetch time) rather than silently duplicated or corrupted.
    """
    rows: list[list] = []
    unparsed: list[tuple] = []
    try:
        opener = gzip.open if _is_gzip(filepath) else open
        with opener(filepath, "rt", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                raw = line.rstrip("\n")
                m = LINE_RE.match(raw)
                if not m:
                    if rows:
                        # Continuation line (e.g. stacktrace) — append to the
                        # previous row's message and raw_line.
                        rows[-1][7] += "\n" + raw
                        rows[-1][10] += "\n" + raw
                    else:
                        unparsed.append((line_no, raw))
                    continue
                ts, level, logger, iflow, message, ip, node = m.groups()
                rows.append([
                    tenant, log_type, filepath.name,
                    ts, level.strip(), logger.strip(),
                    _extract_iflow(iflow), message.strip(),
                    ip.strip(), node.strip(), raw,
                ])
    except Exception:
        pass
    return [tuple(r) for r in rows], unparsed


# ── Tenants ───────────────────────────────────────────────────────────────────

async def upsert_tenant(db: DuckDBConnection, tenant_id: str, name: str, api_url: str,
                        oauth_url: str, client_id: str, client_secret: str):
    def _run(conn):
        db._execute(conn, """
            INSERT INTO tenants (id, name, api_url, oauth_url, client_id, client_secret)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name=excluded.name, api_url=excluded.api_url,
                oauth_url=excluded.oauth_url, client_id=excluded.client_id,
                client_secret=excluded.client_secret
        """, [tenant_id, name, api_url, oauth_url, client_id, client_secret])
    await db.run(_run)


async def get_tenants(db: DuckDBConnection) -> list[dict]:
    return await db.read(db._fetchall_dicts, "SELECT * FROM tenants ORDER BY id")


async def get_tenant(db: DuckDBConnection, tenant_id: str) -> Optional[dict]:
    return await db.read(db._fetchone_dict, "SELECT * FROM tenants WHERE id=?", [tenant_id])


async def delete_tenant(db: DuckDBConnection, tenant_id: str):
    await db.run(db._execute, "DELETE FROM tenants WHERE id=?", [tenant_id])


# ── Fetch Schedules ───────────────────────────────────────────────────────────
async def create_schedule(db: DuckDBConnection, schedule_id: str, name: str,
                           tenants_json: str, log_types_json: str,
                           hours: int, interval_minutes: int, enabled: bool):
    def _run(conn):
        db._execute(conn, """
            INSERT INTO fetch_schedules (id, name, tenants, log_types, hours, interval_minutes, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [schedule_id, name, tenants_json, log_types_json, hours, interval_minutes, enabled])
    await db.run(_run)


async def update_schedule(db: DuckDBConnection, schedule_id: str, name: str,
                           tenants_json: str, log_types_json: str,
                           hours: int, interval_minutes: int, enabled: bool):
    def _run(conn):
        db._execute(conn, """
            UPDATE fetch_schedules
            SET name=?, tenants=?, log_types=?, hours=?, interval_minutes=?, enabled=?
            WHERE id=?
        """, [name, tenants_json, log_types_json, hours, interval_minutes, enabled, schedule_id])
    await db.run(_run)


async def get_schedules(db: DuckDBConnection) -> list[dict]:
    return await db.read(db._fetchall_dicts, "SELECT * FROM fetch_schedules ORDER BY created_at")


async def get_schedule(db: DuckDBConnection, schedule_id: str) -> Optional[dict]:
    return await db.read(db._fetchone_dict, "SELECT * FROM fetch_schedules WHERE id=?", [schedule_id])


async def delete_schedule(db: DuckDBConnection, schedule_id: str):
    await db.run(db._execute, "DELETE FROM fetch_schedules WHERE id=?", [schedule_id])


async def touch_schedule_last_run(db: DuckDBConnection, schedule_id: str):
    await db.run(db._execute,
                 "UPDATE fetch_schedules SET last_run_at=CURRENT_TIMESTAMP WHERE id=?",
                 [schedule_id])


# ── Settings (key/value) ──────────────────────────────────────────────────────
async def get_setting(db: DuckDBConnection, key: str) -> Optional[str]:
    row = await db.read(db._fetchone_dict, "SELECT value FROM settings WHERE key=?", [key])
    return row["value"] if row else None


async def set_setting(db: DuckDBConnection, key: str, value: str):
    def _run(conn):
        db._execute(conn, """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value=excluded.value
        """, [key, value])
    await db.run(_run)


# ── Log Import ────────────────────────────────────────────────────────────────

async def update_file_import_size(db: DuckDBConnection, tenant: str, filename: str, size: int):
    await db.run(db._execute,
        "UPDATE file_imports SET size=? WHERE tenant=? AND filename=?",
        [size, tenant, filename],
    )


async def get_file_import(db: DuckDBConnection, tenant: str, filename: str) -> dict:
    row = await db.read(db._fetchone_dict,
        "SELECT lines, size FROM file_imports WHERE tenant=? AND filename=?",
        [tenant, filename],
    )
    return row if row else {"lines": 0, "size": 0}


# Row count per pyarrow-registered batch. Batching keeps peak memory bounded
# for very large files/imports; pyarrow avoids the per-row Python->DuckDB
# parameter-marshalling cost entirely (see benchmark below).
_INSERT_BATCH_SIZE = 20000
_INSERT_COLUMNS = ["tenant", "log_type", "filename", "timestamp", "level",
                   "logger", "iflow", "message", "ip", "node", "raw_line"]


async def import_rows(db: DuckDBConnection, rows: list[tuple], tenant: str, filename: str,
                       total_lines: Optional[int] = None, size: int = 0) -> int:
    if not rows:
        return 0

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
    #
    # No UNIQUE constraint / ON CONFLICT / in-batch dedupe here anymore (see
    # SCHEMA comment on `logs`): duplicate protection across fetch runs comes
    # entirely from file_imports.lines (only rows past the last-imported line
    # of a file are ever handed to this function). This also fixes a latent
    # correctness issue the old UNIQUE constraint had: legitimate repeated log
    # lines (e.g. heartbeat/timer messages with identical
    # tenant/type/filename/timestamp/level/logger/message within the same
    # second) used to be silently dropped as "duplicates" — they are now
    # imported as the distinct log entries they actually are.
    #
    # `rows` here is only the *new* slice past the last-imported line — but
    # file_imports.lines must record the file's *total* parsed line count so
    # the next incremental fetch computes the correct offset. Callers pass
    # `total_lines` (= previously-imported lines + len(rows)) explicitly;
    # defaulting it to len(rows) is only correct for a brand-new file. Using
    # len(rows) unconditionally here was a bug: on a growing file it kept
    # resetting file_imports.lines back down to just the latest delta, so
    # every subsequent fetch re-imported already-imported lines as "new",
    # silently accumulating real duplicate rows over time.
    if total_lines is None:
        total_lines = len(rows)

    def _run(conn):
        for i in range(0, len(rows), _INSERT_BATCH_SIZE):
            chunk = rows[i:i + _INSERT_BATCH_SIZE]
            columns = list(zip(*chunk))
            arrow_table = pa.table({
                col: pa.array(values, type=pa.string())
                for col, values in zip(_INSERT_COLUMNS, columns)
            })
            view_name = f"_import_batch_{id(chunk)}"
            conn.register(view_name, arrow_table)
            try:
                conn.execute(
                    f"INSERT INTO logs ({','.join(_INSERT_COLUMNS)}) "
                    f"SELECT {','.join(_INSERT_COLUMNS)} FROM {view_name}"
                )
            finally:
                conn.unregister(view_name)
        db._execute(conn, """
            INSERT INTO file_imports (tenant, filename, lines, size) VALUES (?, ?, ?, ?)
            ON CONFLICT (tenant, filename) DO UPDATE SET lines=excluded.lines, size=excluded.size
        """, [tenant, filename, total_lines, size])
        return len(rows)

    return await db.run(_run)


async def import_unparsed_lines(db: DuckDBConnection, unparsed: list[tuple],
                                 tenant: str, log_type: str, filename: str):
    """Persist lines that parse_log_file() couldn't attribute to any parsed
    row (see parse_log_file docstring) instead of silently dropping them."""
    if not unparsed:
        return

    def _run(conn):
        for line_no, raw_text in unparsed:
            db._execute(conn, """
                INSERT INTO unparsed_lines (tenant, log_type, filename, line_no, raw_text)
                VALUES (?, ?, ?, ?, ?)
            """, [tenant, log_type, filename, line_no, raw_text])

    await db.run(_run)


# ── Query ─────────────────────────────────────────────────────────────────────

async def query_logs(
    db: DuckDBConnection, *,
    tenant: Optional[str] = None,
    level: Optional[str] = None,
    iflow: Optional[str] = None,
    grep: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
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
        items = db._fetchall_dicts(cur,
            f"SELECT * FROM logs {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (params + [page_size, offset]) or None,
        )

        return {
            "total": total or 0,
            "page": page,
            "page_size": page_size,
            "pages": max(1, ((total or 0) + page_size - 1) // page_size),
            "items": items,
        }

    return await db.read(_run)


async def get_log_entry(db: DuckDBConnection, entry_id: int) -> Optional[dict]:
    return await db.read(db._fetchone_dict, "SELECT * FROM logs WHERE id=?", [entry_id])


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats(db: DuckDBConnection, tenant: Optional[str] = None) -> dict:
    def _run(cur):
        where = "WHERE tenant=?" if tenant and tenant != "all" else ""
        params = [tenant] if tenant and tenant != "all" else []

        levels = db._fetchall_dicts(cur,
            f"SELECT upper(level) as lvl, COUNT(*) as cnt FROM logs {where} "
            f"GROUP BY lvl ORDER BY cnt DESC",
            params or None,
        )

        err_params = [tenant] if tenant and tenant != "all" else []
        err_where  = "AND tenant=?" if tenant and tenant != "all" else ""
        top_errors = db._fetchall_dicts(cur,
            f"SELECT iflow, COUNT(*) as cnt FROM logs "
            f"WHERE upper(level)='ERROR' {err_where} "
            f"GROUP BY iflow ORDER BY cnt DESC LIMIT 15",
            err_params or None,
        )

        timeline = db._fetchall_dicts(cur,
            f"SELECT strftime(timestamp, '%Y-%m-%d %H') as hour, COUNT(*) as cnt "
            f"FROM logs {where} "
            f"{'AND' if where else 'WHERE'} upper(level)='ERROR' "
            f"AND timestamp >= (CURRENT_TIMESTAMP - INTERVAL '48 hours') "
            f"GROUP BY hour ORDER BY hour",
            params or None,
        )

        total = db._fetchone_val(cur, f"SELECT COUNT(*) as cnt FROM logs {where}", params or None)

        per_tenant = db._fetchall_dicts(cur,
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


async def cleanup_old_logs(db: DuckDBConnection, older_than_days: int, tenant: Optional[str] = None) -> dict:
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
    return await db.run(_run)

# NOTE: a dedupe-by-content function (matching on tenant/log_type/filename/
# timestamp/level/logger/message) was considered and deliberately rejected —
# see the comment on import_rows() above: CPI logs legitimately contain
# repeated messages with identical timestamp/level/logger/message text
# (e.g. heartbeats, generic per-second status lines) that are NOT
# duplicates. There is no reliable content-based uniqueness key available;
# the only correct duplicate protection is file_imports.lines (line-offset
# tracking per source file), which is already in place.


