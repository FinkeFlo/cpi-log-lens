"""SAP Cloud Integration API access."""

from app.cpi.client import CpiClient

# A tenant whose URLs use this scheme imports the bundled sample logs instead
# of calling SAP CPI, so new users can try the app without credentials.
DEMO_URL = "demo://"


def is_demo(tenant: dict) -> bool:
    return tenant["api_url"].startswith(DEMO_URL)


def client_for(tenant: dict, *, timeout: float | None = None) -> CpiClient:
    """The client for a tenant's CPI API."""
    kw: dict = {} if timeout is None else {"timeout": timeout}
    return CpiClient(tenant["api_url"], tenant["oauth_url"], tenant["client_id"], tenant["client_secret"], **kw)
