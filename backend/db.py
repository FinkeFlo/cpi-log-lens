"""Database layer — SQLite via aiosqlite."""
import re
import gzip
import sqlite3
import aiosqlite
from pathlib import Path
from typing import Optional

DB_PATH: Path = Path("/data/cpi_logs.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    api_url     TEXT NOT NULL,
    oauth_url   TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY,
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
);

CREATE INDEX IF NOT EXISTS idx_logs_tenant    ON logs(tenant);
CREATE INDEX IF NOT EXISTS idx_logs_level     ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_iflow     ON logs(iflow);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_filename  ON logs(tenant, filename);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id          INTEGER PRIMARY KEY,
    tenant      TEXT,
    log_type    TEXT,
    started_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    files_total INTEGER DEFAULT 0,
    files_done  INTEGER DEFAULT 0,
    entries_imported INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'running'
);
"""

LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})#[^#]*#([^#]*)#([^#]*)#[^#]*#([^#]*)#[^#]*'
    r'#[^#]*#[^#]*#[^#]*#[^#]*#(.*?)#-#([^#]*)#([^\n]*)$'
)


async def init_db(path: Path = DB_PATH):
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_db(path: Path = DB_PATH):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


# ── Tenants ───────────────────────────────────────────────────────────────────

async def upsert_tenant(db, tenant_id: str, name: str, api_url: str,
                        oauth_url: str, client_id: str, client_secret: str):
    await db.execute("""
        INSERT INTO tenants (id, name, api_url, oauth_url, client_id, client_secret)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, api_url=excluded.api_url,
            oauth_url=excluded.oauth_url, client_id=excluded.client_id,
            client_secret=excluded.client_secret
    """, (tenant_id, name, api_url, oauth_url, client_id, client_secret))
    await db.commit()


async def get_tenants(db) -> list[dict]:
    async with db.execute("SELECT * FROM tenants ORDER BY id") as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_tenant(db, tenant_id: str) -> Optional[dict]:
    async with db.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def delete_tenant(db, tenant_id: str):
    await db.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
    await db.commit()


# ── Log Import ────────────────────────────────────────────────────────────────

def _is_gzip(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except Exception:
        return False


def parse_log_file(tenant: str, log_type: str, filepath: Path) -> list[tuple]:
    """Parse a log file (gzip or plain) and return list of row tuples."""
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
                    iflow.strip(), message.strip(),
                    ip.strip(), node.strip(),
                ))
    except Exception:
        pass
    return rows


async def file_already_imported(db, tenant: str, filename: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM logs WHERE tenant=? AND filename=? LIMIT 1",
        (tenant, filename)
    ) as cur:
        return (await cur.fetchone()) is not None


async def import_rows(db, rows: list[tuple]) -> int:
    if not rows:
        return 0
    await db.executemany(
        "INSERT INTO logs (tenant,log_type,filename,timestamp,level,logger,iflow,message,ip,node) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    await db.commit()
    return len(rows)


# ── Query ─────────────────────────────────────────────────────────────────────

async def query_logs(
    db, *,
    tenant: Optional[str] = None,
    level: Optional[str] = None,
    iflow: Optional[str] = None,
    grep: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    conditions, params = [], []

    if tenant and tenant != "all":
        conditions.append("tenant = ?")
        params.append(tenant)
    if level and level != "ALL":
        conditions.append("UPPER(level) = ?")
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

    async with db.execute(f"SELECT COUNT(*) FROM logs {where}", params) as cur:
        total = (await cur.fetchone())[0]

    offset = (page - 1) * page_size
    sql = f"SELECT * FROM logs {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    async with db.execute(sql, params + [page_size, offset]) as cur:
        rows = await cur.fetchall()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "items": [dict(r) for r in rows],
    }


async def get_log_entry(db, entry_id: int) -> Optional[dict]:
    async with db.execute("SELECT * FROM logs WHERE id=?", (entry_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats(db, tenant: Optional[str] = None) -> dict:
    where = "WHERE tenant=?" if tenant and tenant != "all" else ""
    params = [tenant] if tenant and tenant != "all" else []

    # Level distribution
    async with db.execute(
        f"SELECT UPPER(level) as lvl, COUNT(*) as cnt FROM logs {where} GROUP BY lvl ORDER BY cnt DESC",
        params
    ) as cur:
        levels = [dict(r) for r in await cur.fetchall()]

    # Top IFlows by error
    error_where = ("WHERE " + " AND ".join(filter(None, [
        "UPPER(level)='ERROR'",
        "tenant=?" if tenant and tenant != "all" else "",
    ]))) if True else ""
    error_params = ([tenant] if tenant and tenant != "all" else [])

    async with db.execute(
        f"SELECT iflow, COUNT(*) as cnt FROM logs "
        f"WHERE UPPER(level)='ERROR' {'AND tenant=?' if tenant and tenant != 'all' else ''} "
        f"GROUP BY iflow ORDER BY cnt DESC LIMIT 15",
        error_params,
    ) as cur:
        top_errors = [dict(r) for r in await cur.fetchall()]

    # Errors per hour (last 48h)
    async with db.execute(
        f"SELECT SUBSTR(timestamp,1,13) as hour, COUNT(*) as cnt "
        f"FROM logs {where} {'AND' if where else 'WHERE'} UPPER(level)='ERROR' "
        f"AND timestamp >= datetime('now','-48 hours') "
        f"GROUP BY hour ORDER BY hour",
        params,
    ) as cur:
        timeline = [dict(r) for r in await cur.fetchall()]

    # DB totals
    async with db.execute(f"SELECT COUNT(*) as cnt FROM logs {where}", params) as cur:
        total = (await cur.fetchone())["cnt"]

    async with db.execute(
        f"SELECT tenant, log_type, COUNT(*) as cnt, MAX(timestamp) as last_ts "
        f"FROM logs {where} GROUP BY tenant, log_type ORDER BY tenant",
        params,
    ) as cur:
        per_tenant = [dict(r) for r in await cur.fetchall()]

    return {
        "total": total,
        "levels": levels,
        "top_errors": top_errors,
        "timeline": timeline,
        "per_tenant": per_tenant,
    }


# ── DB Info ───────────────────────────────────────────────────────────────────

async def get_db_info(db, db_path: Path) -> dict:
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    async with db.execute("SELECT COUNT(*) as cnt FROM logs") as cur:
        entries = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COUNT(*) as cnt FROM tenants") as cur:
        tenant_count = (await cur.fetchone())["cnt"]
    return {
        "path": str(db_path),
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1024 / 1024, 1),
        "entries": entries,
        "tenants": tenant_count,
    }


async def clear_db(db):
    await db.execute("DELETE FROM logs")
    await db.execute("DELETE FROM fetch_runs")
    await db.commit()
