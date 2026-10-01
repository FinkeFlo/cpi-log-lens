"""Import bookkeeping: rows imported per (tenant, log type, file name) and the file size seen."""

from app.repositories.database import Database


async def update_file_import_size(db: Database, tenant: str, log_type: str, filename: str, size: int):
    await db.run(
        db.execute,
        "UPDATE file_imports SET size=? WHERE tenant=? AND log_type=? AND filename=?",
        [size, tenant, log_type, filename],
    )


async def get_file_import(db: Database, tenant: str, log_type: str, filename: str) -> dict:
    # Fetch-job bookkeeping goes through the writer (which the job needs
    # anyway) so a read pool saturated by the UI can never fail a running job.
    row = await db.run(
        db.fetch_one,
        "SELECT lines, size FROM file_imports WHERE tenant=? AND log_type=? AND filename=?",
        [tenant, log_type, filename],
    )
    return row if row else {"lines": 0, "size": 0}


def save_lines(conn, tenant: str, log_type: str, filename: str, lines: int) -> None:
    """Record the rows imported so far (inside the caller's transaction)."""
    Database.execute(
        conn,
        """
        INSERT INTO file_imports (tenant, log_type, filename, lines, size) VALUES (?, ?, ?, ?, 0)
        ON CONFLICT (tenant, log_type, filename) DO UPDATE SET lines=excluded.lines
    """,
        [tenant, log_type, filename, lines],
    )


def save_complete(conn, tenant: str, log_type: str, filename: str, lines: int, size: int) -> None:
    """Record a fully parsed file: its row count and the remote size it had."""
    Database.execute(
        conn,
        """
        INSERT INTO file_imports (tenant, log_type, filename, lines, size) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (tenant, log_type, filename) DO UPDATE SET lines=excluded.lines, size=excluded.size
    """,
        [tenant, log_type, filename, lines, size],
    )
