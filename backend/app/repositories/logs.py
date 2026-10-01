"""Log entries: bulk insert, list/detail queries, statistics and deletion."""

from pathlib import Path

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


async def query_logs(
    db: Database,
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

        total = db.fetch_val(cur, f"SELECT COUNT(*) FROM logs {where}", params or None)

        offset = (page - 1) * page_size
        items = db.fetch_all(
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
            f"GROUP BY iflow ORDER BY cnt DESC LIMIT 15",
            err_params or None,
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
