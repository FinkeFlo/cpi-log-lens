"""History of fetch jobs."""

import json
from datetime import UTC, datetime

from app.repositories.database import Database

# The newest runs that are kept; older ones are deleted when a run starts.
KEEP_RUNS = 1000

_COLUMNS = (
    "id, trigger, params, status, started_at, finished_at, "
    "files_total, files_done, rows_imported, warnings, errors, error"
)


def utc(value: datetime) -> datetime:
    """Timestamps are stored as UTC without time zone."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _row(row: dict | None) -> dict | None:
    if row is not None:
        row["params"] = json.loads(row["params"])
        for key in ("started_at", "finished_at"):
            if row[key] is not None:
                row[key] = row[key].replace(tzinfo=UTC)
    return row


async def insert_run(db: Database, run_id: str, trigger: str, params: dict, started_at: datetime) -> None:
    def _run(conn):
        db.execute(
            conn,
            "INSERT INTO fetch_runs (id, trigger, params, status, started_at) VALUES (?, ?, ?, 'running', ?)",
            [run_id, trigger, json.dumps(params), utc(started_at)],
        )
        db.execute(
            conn,
            "DELETE FROM fetch_runs WHERE id NOT IN (SELECT id FROM fetch_runs ORDER BY started_at DESC LIMIT ?)",
            [KEEP_RUNS],
        )

    await db.run(_run)


async def update_run(db: Database, run_id: str, **fields) -> None:
    """Set the given columns (status, finished_at, counters, error) of a run."""
    values = [utc(v) if isinstance(v, datetime) else v for v in fields.values()]
    assignments = ", ".join(f"{name} = ?" for name in fields)
    await db.run(db.execute, f"UPDATE fetch_runs SET {assignments} WHERE id = ?", [*values, run_id])


async def mark_interrupted(db: Database) -> int:
    """Runs still marked running were cut off by a stop or crash of the app."""

    def _run(conn) -> int:
        n = db.fetch_val(conn, "SELECT count(*) FROM fetch_runs WHERE status = 'running'")
        db.execute(
            conn,
            "UPDATE fetch_runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
            [utc(datetime.now(UTC))],
        )
        return n

    return await db.run(_run)


async def list_runs(db: Database, limit: int) -> list[dict]:
    rows = await db.read(db.fetch_all, f"SELECT {_COLUMNS} FROM fetch_runs ORDER BY started_at DESC LIMIT ?", [limit])
    return [r for r in (_row(r) for r in rows) if r is not None]


async def get_run(db: Database, run_id: str) -> dict | None:
    return _row(await db.read(db.fetch_one, f"SELECT {_COLUMNS} FROM fetch_runs WHERE id = ?", [run_id]))
