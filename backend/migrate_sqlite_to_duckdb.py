"""
Migrate existing SQLite database to DuckDB.

Usage:
    python3 migrate_sqlite_to_duckdb.py \
        --sqlite ../data/cpi_logs.db \
        --duckdb ../data/cpi_logs.duckdb

The SQLite file is NOT modified or deleted.
"""
import argparse
import sqlite3
import time
import duckdb
from pathlib import Path

SCHEMA_STMTS = [
    """CREATE TABLE IF NOT EXISTS tenants (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        api_url       TEXT NOT NULL,
        oauth_url     TEXT NOT NULL,
        client_id     TEXT NOT NULL,
        client_secret TEXT NOT NULL,
        created_at    TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS logs (
        id          BIGINT PRIMARY KEY,
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_logs_tenant       ON logs(tenant)",
    "CREATE INDEX IF NOT EXISTS idx_logs_level        ON logs(level)",
    "CREATE INDEX IF NOT EXISTS idx_logs_iflow        ON logs(iflow)",
    "CREATE INDEX IF NOT EXISTS idx_logs_timestamp    ON logs(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_logs_tenant_ts    ON logs(tenant, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_logs_level_ts     ON logs(level, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_logs_tenant_level ON logs(tenant, level, timestamp DESC)",
    """CREATE TABLE IF NOT EXISTS file_imports (
        tenant   TEXT NOT NULL,
        filename TEXT NOT NULL,
        lines    INTEGER NOT NULL DEFAULT 0,
        size     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant, filename)
    )""",
    """CREATE TABLE IF NOT EXISTS fetch_runs (
        id               BIGINT PRIMARY KEY,
        tenant           TEXT,
        log_type         TEXT,
        started_at       TEXT DEFAULT CURRENT_TIMESTAMP,
        finished_at      TEXT,
        files_total      INTEGER DEFAULT 0,
        files_done       INTEGER DEFAULT 0,
        entries_imported INTEGER DEFAULT 0,
        status           TEXT DEFAULT 'running'
    )""",
]

BATCH_SIZE = 50_000


def migrate(sqlite_path: Path, duckdb_path: Path):
    print(f"Source : {sqlite_path}  ({sqlite_path.stat().st_size / 1024**3:.2f} GB)")
    print(f"Target : {duckdb_path}")
    print()

    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    dst = duckdb.connect(str(duckdb_path))

    # Create schema
    print("Creating schema …")
    for stmt in SCHEMA_STMTS:
        dst.execute(stmt)
    print("  done.\n")

    # ── tenants ──────────────────────────────────────────────────────────────
    print("Migrating tenants …")
    rows = src.execute("SELECT * FROM tenants").fetchall()
    for r in rows:
        dst.execute("""
            INSERT INTO tenants (id, name, api_url, oauth_url, client_id, client_secret, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
        """, list(r))
    print(f"  {len(rows)} tenants migrated.\n")

    # ── file_imports ──────────────────────────────────────────────────────────
    print("Migrating file_imports …")
    # Fetch actual columns from SQLite (may differ from current schema)
    fi_cols = [r[1] for r in src.execute("PRAGMA table_info(file_imports)").fetchall()]
    rows = src.execute(f"SELECT {', '.join(fi_cols)} FROM file_imports").fetchall()
    for r in rows:
        row_dict = dict(zip(fi_cols, r))
        dst.execute("""
            INSERT INTO file_imports (tenant, filename, lines, size)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (tenant, filename) DO NOTHING
        """, [row_dict["tenant"], row_dict["filename"],
              row_dict.get("lines", 0), row_dict.get("size", 0)])
    print(f"  {len(rows)} file_import records migrated.\n")

    # ── logs (batched) ────────────────────────────────────────────────────────
    total = src.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    print(f"Migrating logs ({total:,} rows) in batches of {BATCH_SIZE:,} …")

    cursor = src.execute(
        "SELECT id,tenant,log_type,filename,timestamp,level,logger,"
        "iflow,message,ip,node,imported_at FROM logs ORDER BY id"
    )

    migrated = 0
    t0 = time.time()
    while True:
        batch = cursor.fetchmany(BATCH_SIZE)
        if not batch:
            break
        dst.executemany("""
            INSERT OR IGNORE INTO logs
            (id,tenant,log_type,filename,timestamp,level,logger,iflow,message,ip,node,imported_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, [list(r) for r in batch])
        migrated += len(batch)
        elapsed = time.time() - t0
        pct = migrated / total * 100
        rate = migrated / elapsed if elapsed > 0 else 0
        eta  = (total - migrated) / rate if rate > 0 else 0
        print(f"  {migrated:>12,} / {total:,}  ({pct:.1f}%)  "
              f"{rate:,.0f} rows/s  ETA {eta:.0f}s", end="\r")

    print(f"\n  {migrated:,} log rows migrated in {time.time()-t0:.1f}s.\n")

    src.close()
    dst.close()

    new_size = duckdb_path.stat().st_size
    print(f"Done! DuckDB size: {new_size / 1024**3:.2f} GB  "
          f"({new_size / sqlite_path.stat().st_size * 100:.0f}% of SQLite)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--duckdb", required=True, type=Path)
    args = parser.parse_args()

    if not args.sqlite.exists():
        print(f"ERROR: SQLite file not found: {args.sqlite}")
        raise SystemExit(1)
    if args.duckdb.exists():
        print(f"ERROR: DuckDB file already exists: {args.duckdb}  (delete it first)")
        raise SystemExit(1)

    migrate(args.sqlite, args.duckdb)
