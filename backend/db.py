"""Database layer — DuckDB."""
import re
import gzip
import asyncio
import duckdb
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
    timestamp   TEXT NOT NULL,
    level       TEXT,
    logger      TEXT,
    iflow       TEXT,
    message     TEXT,
    ip          TEXT,
    node        TEXT,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant, log_type, filename, timestamp, level, logger, message)
);

CREATE INDEX IF NOT EXISTS idx_logs_tenant    ON logs(tenant);
CREATE INDEX IF NOT EXISTS idx_logs_level     ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_iflow     ON logs(iflow);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_tenant_ts ON logs(tenant, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_logs_level_ts  ON logs(level, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_logs_tenant_level ON logs(tenant, level, timestamp DESC);

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
"""


READ_POOL_SIZE = 4


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
        self._conn = duckdb.connect(str(path))
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
        row = c.fetchone()
        return dict(zip(cols, row)) if row else None

    @classmethod
    def _fetchone_val(cls, cur, sql: str, params=None):
        c = cls._execute(cur, sql, params)
        row = c.fetchone()
        return row[0] if row else None

    async def run(self, fn, *args, **kwargs):
        """Write path: run a synchronous DB function against the main connection,
        serialized by `_write_lock`, off the event loop (in a worker thread) so a
        long-running write never blocks other requests from being scheduled."""
        async with self._write_lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: fn(self._conn, *args, **kwargs))

    async def read(self, fn, *args, **kwargs):
        """Read path: borrow a cursor from the pool and run a synchronous SELECT
        against it, off the event loop. Runs concurrently with `run()` writes and
        with other `read()` calls (bounded by READ_POOL_SIZE)."""
        cur = await self._read_pool.get()
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: fn(cur, *args, **kwargs))
        finally:
            self._read_pool.put_nowait(cur)

    async def close(self):
        pass  # singleton — stays open for the lifetime of the process


# ── Singleton connection ──────────────────────────────────────────────────────
_db_instance: Optional[DuckDBConnection] = None


async def init_db(path: Path = DB_PATH):
    global _db_instance
    conn = duckdb.connect(str(path))
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
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


def parse_log_file(tenant: str, log_type: str, filepath: Path) -> list[tuple]:
    rows = []
    try:
        opener = gzip.open if _is_gzip(filepath) else open
        with opener(filepath, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LINE_RE.match(line.rstrip("\n"))
                if not m:
                    continue
                ts, level, logger, iflow, message, ip, node = m.groups()
                rows.append((
                    tenant, log_type, filepath.name,
                    ts, level.strip(), logger.strip(),
                    _extract_iflow(iflow), message.strip(),
                    ip.strip(), node.strip(),
                ))
    except Exception:
        pass
    return rows


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


async def import_rows(db: DuckDBConnection, rows: list[tuple], tenant: str, filename: str, size: int = 0) -> int:
    if not rows:
        return 0

    def _run(conn):
        before = db._fetchone_val(conn, "SELECT COUNT(*) FROM logs")
        conn.executemany(
            "INSERT INTO logs "
            "(tenant,log_type,filename,timestamp,level,logger,iflow,message,ip,node) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            rows,
        )
        after = db._fetchone_val(conn, "SELECT COUNT(*) FROM logs")
        inserted = (after or 0) - (before or 0)
        db._execute(conn, """
            INSERT INTO file_imports (tenant, filename, lines, size) VALUES (?, ?, ?, ?)
            ON CONFLICT (tenant, filename) DO UPDATE SET lines=excluded.lines, size=excluded.size
        """, [tenant, filename, len(rows), size])
        return inserted

    return await db.run(_run)


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
            conditions.append("timestamp >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("timestamp <= ?")
            params.append(date_to + " 23:59:59")

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
            f"SELECT strftime(timestamp::TIMESTAMP, '%Y-%m-%d %H') as hour, COUNT(*) as cnt "
            f"FROM logs {where} "
            f"{'AND' if where else 'WHERE'} upper(level)='ERROR' "
            f"AND timestamp >= (CURRENT_TIMESTAMP - INTERVAL '48 hours')::TEXT "
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
    await db.run(_run)


async def cleanup_old_logs(db: DuckDBConnection, older_than_days: int, tenant: Optional[str] = None) -> dict:
    def _run(conn):
        conditions = [f"timestamp < (CURRENT_TIMESTAMP - INTERVAL '{older_than_days} days')::TEXT"]
        params = []
        if tenant and tenant != "all":
            conditions.append("tenant = ?")
            params.append(tenant)
        where = "WHERE " + " AND ".join(conditions)
        to_delete = db._fetchone_val(conn, f"SELECT COUNT(*) FROM logs {where}", params or None) or 0
        db._execute(conn, f"DELETE FROM logs {where}", params or None)
        remaining = db._fetchone_val(conn, "SELECT COUNT(*) FROM logs") or 0
        return {"deleted": to_delete, "remaining": remaining}
    return await db.run(_run)
