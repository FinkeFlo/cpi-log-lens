"""SAP Cloud Integration API access."""

import httpx

from app.config import get_settings
from app.cpi.client import CpiClient
from app.cpi.fake import FAKE_URL, sample_cpi

# A tenant whose URLs use this scheme imports the bundled sample logs from the
# fake CPI server, so new users can try the app without credentials.
DEMO_URL = "demo://"


def is_demo(tenant: dict) -> bool:
    return tenant["api_url"].startswith(DEMO_URL)


def client_for(tenant: dict, *, timeout: httpx.Timeout | float | None = None, retries: int | None = None) -> CpiClient:
    """The client for a tenant: the real CPI API, or the fake one with the sample
    logs for the demo tenant and in MOCK mode. `timeout` and `retries` (attempts per
    request) replace the defaults for downloads."""
    kw: dict = {}
    if timeout is not None:
        kw["timeout"] = timeout
    if retries is not None:
        kw["retries"] = retries
    if get_settings().mock or is_demo(tenant):
        transport = httpx.ASGITransport(app=sample_cpi().app)
        return CpiClient(FAKE_URL, f"{FAKE_URL}/oauth/token", "demo", "demo", transport=transport, **kw)
    return CpiClient(tenant["api_url"], tenant["oauth_url"], tenant["client_id"], tenant["client_secret"], **kw)
