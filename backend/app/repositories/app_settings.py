"""Key/value settings stored in the database (e.g. the default fetch form)."""

from app.repositories.database import Database


async def get_setting(db: Database, key: str) -> str | None:
    row = await db.read(db.fetch_one, "SELECT value FROM settings WHERE key=?", [key])
    return row["value"] if row else None


async def set_setting(db: Database, key: str, value: str):
    def _run(conn):
        db.execute(
            conn,
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value=excluded.value
        """,
            [key, value],
        )

    await db.run(_run)
