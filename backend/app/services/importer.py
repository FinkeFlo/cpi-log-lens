"""Import of a downloaded log file: parse it and insert the rows past the stored offset."""

from pathlib import Path

from app.parsing.cpi_log import iter_log_batches
from app.repositories import file_imports as file_imports_repo
from app.repositories import logs as logs_repo
from app.repositories.database import Database
from app.services import stats

# Row count per parsed/inserted batch. Batching keeps peak memory bounded
# for very large files; pyarrow avoids the per-row Python->DuckDB
# parameter-marshalling cost entirely (see logs.insert_rows).
INSERT_BATCH_SIZE = 20000


async def import_log_file(
    db: Database, tenant: str, log_type: str, filepath: Path, filename: str, already: int, size: int = 0
) -> int:
    """Parse `filepath` and import every row past the first `already` rows.
    Returns the number of newly inserted rows.

    Parsing and inserting both run batch by batch in the single DB writer
    thread: the event loop never runs CPU-bound parsing, and memory is bounded
    by one batch instead of the whole file.

    Duplicate protection: there is no UNIQUE constraint / ON CONFLICT /
    dedupe on `logs` (see repositories/schema.py). Protection across fetch runs comes
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
        for rows, unparsed in iter_log_batches(tenant, log_type, filepath, INSERT_BATCH_SIZE):
            skip = max(0, already - total)
            total += len(rows)
            new = rows[skip:]
            if not new and not (unparsed and already == 0):
                continue
            conn.begin()
            try:
                if new:
                    logs_repo.insert_rows(conn, new)
                    file_imports_repo.save_lines(conn, tenant, filename, total)
                if unparsed and already == 0:
                    logs_repo.insert_unparsed(conn, tenant, log_type, filename, unparsed)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            inserted += len(new)
            stats.invalidate()
        if total:
            file_imports_repo.save_complete(conn, tenant, filename, max(total, already), size)
        return inserted

    return await db.run(_run)
