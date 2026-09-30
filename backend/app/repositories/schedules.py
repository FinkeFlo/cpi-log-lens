"""Recurring fetch schedules. tenants/log_types are stored as JSON arrays."""

from app.repositories.database import Database


async def create_schedule(
    db: Database,
    schedule_id: str,
    name: str,
    tenants_json: str,
    log_types_json: str,
    hours: int,
    interval_minutes: int,
    enabled: bool,
):
    def _run(conn):
        db.execute(
            conn,
            """
            INSERT INTO fetch_schedules (id, name, tenants, log_types, hours, interval_minutes, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            [schedule_id, name, tenants_json, log_types_json, hours, interval_minutes, enabled],
        )

    await db.run(_run)


async def update_schedule(
    db: Database,
    schedule_id: str,
    name: str,
    tenants_json: str,
    log_types_json: str,
    hours: int,
    interval_minutes: int,
    enabled: bool,
):
    def _run(conn):
        db.execute(
            conn,
            """
            UPDATE fetch_schedules
            SET name=?, tenants=?, log_types=?, hours=?, interval_minutes=?, enabled=?
            WHERE id=?
        """,
            [name, tenants_json, log_types_json, hours, interval_minutes, enabled, schedule_id],
        )

    await db.run(_run)


async def get_schedules(db: Database) -> list[dict]:
    return await db.read(db.fetch_all, "SELECT * FROM fetch_schedules ORDER BY created_at")


async def get_schedule(db: Database, schedule_id: str) -> dict | None:
    return await db.read(db.fetch_one, "SELECT * FROM fetch_schedules WHERE id=?", [schedule_id])


async def delete_schedule(db: Database, schedule_id: str):
    await db.run(db.execute, "DELETE FROM fetch_schedules WHERE id=?", [schedule_id])


async def touch_schedule_last_run(db: Database, schedule_id: str):
    await db.run(db.execute, "UPDATE fetch_schedules SET last_run_at=CURRENT_TIMESTAMP WHERE id=?", [schedule_id])
