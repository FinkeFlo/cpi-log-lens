"""Database backups into BACKUP_DIR (default: `backups` next to the database file)."""

import logging
import shutil
import time
from datetime import UTC, datetime

from fastapi import HTTPException

from app import storage
from app.config import backup_dir, get_settings
from app.repositories.database import Database

log = logging.getLogger("cpi.db")


async def create_backup(db: Database) -> dict:
    """Copy the database into a new file while the app keeps running. Holds the
    database writer for the duration, so imports wait; reads continue."""
    settings = get_settings()
    target_dir = backup_dir(settings)
    target_dir.mkdir(parents=True, exist_ok=True)
    wal = settings.db_path.with_name(settings.db_path.name + ".wal")
    needed = settings.db_path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    free = shutil.disk_usage(target_dir).free
    if free < needed * 1.1:
        raise HTTPException(507, f"Not enough free disk space in {target_dir}: {needed} bytes needed, {free} free.")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = target_dir / f"{settings.db_path.stem}-{stamp}.duckdb"
    t0 = time.perf_counter()
    await db.run(storage.backup_to, dest)
    duration = time.perf_counter() - t0
    size = dest.stat().st_size
    log.info("backup written to %s (%.1f MB) in %.1fs", dest, size / 1e6, duration)
    return {"path": str(dest), "size_bytes": size, "duration_s": round(duration, 1)}


def list_backups() -> list[dict]:
    target_dir = backup_dir(get_settings())
    if not target_dir.is_dir():
        return []
    files = sorted(target_dir.glob("*.duckdb"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "size_bytes": p.stat().st_size,
            "created_at": datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(timespec="seconds"),
        }
        for p in files
    ]
