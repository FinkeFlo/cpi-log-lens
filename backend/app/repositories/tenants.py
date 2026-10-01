"""Tenants (CPI connections)."""

import json

from app.repositories.database import Database


async def upsert_tenant(
    db: Database, tenant_id: str, name: str, api_url: str, oauth_url: str, client_id: str, client_secret: str
):
    def _run(conn):
        db.execute(
            conn,
            """
            INSERT INTO tenants (id, name, api_url, oauth_url, client_id, client_secret)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name=excluded.name, api_url=excluded.api_url,
                oauth_url=excluded.oauth_url, client_id=excluded.client_id,
                client_secret=excluded.client_secret
        """,
            [tenant_id, name, api_url, oauth_url, client_id, client_secret],
        )

    await db.run(_run)


async def get_tenants(db: Database) -> list[dict]:
    return await db.read(db.fetch_all, "SELECT * FROM tenants ORDER BY id")


async def get_tenant(db: Database, tenant_id: str) -> dict | None:
    return await db.read(db.fetch_one, "SELECT * FROM tenants WHERE id=?", [tenant_id])


async def delete_tenant(db: Database, tenant_id: str, *, purge: bool) -> dict:
    """Delete a tenant in one transaction. Schedules no longer name it, and a
    schedule left without tenants is deleted. With `purge`, its log entries,
    import bookkeeping and unparsed lines are deleted as well; without, they are
    kept together, so a tenant created again with the same id continues where
    it stopped instead of importing its files a second time."""

    def _run(conn) -> dict:
        conn.begin()
        try:
            db.execute(conn, "DELETE FROM tenants WHERE id=?", [tenant_id])
            deleted_entries = 0
            if purge:
                deleted_entries = db.fetch_val(conn, "SELECT count(*) FROM logs WHERE tenant=?", [tenant_id])
                for table in ("logs", "file_imports", "unparsed_lines"):
                    db.execute(conn, f"DELETE FROM {table} WHERE tenant=?", [tenant_id])
            schedules_changed = schedules_deleted = 0
            for row in db.fetch_all(conn, "SELECT id, tenants FROM fetch_schedules"):
                tenants = json.loads(row["tenants"])
                if tenant_id not in tenants:
                    continue
                rest = [t for t in tenants if t != tenant_id]
                if rest:
                    db.execute(conn, "UPDATE fetch_schedules SET tenants=? WHERE id=?", [json.dumps(rest), row["id"]])
                    schedules_changed += 1
                else:
                    db.execute(conn, "DELETE FROM fetch_schedules WHERE id=?", [row["id"]])
                    schedules_deleted += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return {
            "deleted_entries": deleted_entries,
            "schedules_changed": schedules_changed,
            "schedules_deleted": schedules_deleted,
        }

    return await db.run(_run)
