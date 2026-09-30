"""Tenants (CPI connections)."""

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


async def delete_tenant(db: Database, tenant_id: str):
    await db.run(db.execute, "DELETE FROM tenants WHERE id=?", [tenant_id])
