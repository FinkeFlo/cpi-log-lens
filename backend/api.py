"""CPI API client — OAuth2 + Log file fetching."""
import asyncio
import re
import httpx
from typing import Optional

# Per-download-request timeouts and retry-with-backoff for the CPI LogFiles
# API: individual file downloads have been observed taking 30-90s (server-side
# decompression/streaming, see plan.md Phase 1), so timeouts must be generous
# and transient errors (timeouts, 5xx, connection resets) should be retried
# instead of failing the whole fetch job over a single flaky request.
DOWNLOAD_TIMEOUT = httpx.Timeout(180.0, connect=15.0)
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0  # seconds; exponential: 2s, 4s, 8s


def epoch_ms(date_str: str) -> int:
    """Extract epoch ms from OData /Date(1786543619000)/ format."""
    m = re.search(r"\d+", date_str or "")
    return int(m.group()) if m else 0


def make_client() -> httpx.AsyncClient:
    """Create a single shared client for a fetch job (per tenant), reused
    across the token call, the file-list call and every file download.
    Reuses the underlying TCP/TLS connection (keep-alive) instead of a new
    connection per request."""
    return httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
    )


async def _retry(fn, *, what: str):
    """Run an async CPI-API call, retrying transient failures with
    exponential backoff. Raises the last exception if all attempts fail."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await fn()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
        except httpx.HTTPStatusError as e:
            # Retry on transient server errors / rate limiting, not on 4xx
            # client errors (auth, not-found, bad-request) which won't
            # succeed on retry.
            if e.response.status_code in (429, 500, 502, 503, 504):
                last_exc = e
            else:
                raise
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
    raise last_exc


async def get_token(
    oauth_url: str, client_id: str, client_secret: str, client: Optional[httpx.AsyncClient] = None
) -> str:
    async def _do():
        c = client or httpx.AsyncClient(timeout=30)
        owns = client is None
        try:
            resp = await c.post(
                oauth_url,
                params={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
            )
            resp.raise_for_status()
            return resp.json()["access_token"]
        finally:
            if owns:
                await c.aclose()

    return await _retry(_do, what="get_token")


async def list_remote_files(
    api_url: str, token: str, log_type: str, client: Optional[httpx.AsyncClient] = None
) -> list[dict]:
    filter_str = f"LogFileType eq '{log_type}' and NodeScope eq 'worker'"

    async def _do():
        c = client or httpx.AsyncClient(timeout=90)
        owns = client is None
        try:
            resp = await c.get(
                f"{api_url}/api/v1/LogFiles",
                params={"$filter": filter_str},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json().get("d", {}).get("results", [])
        finally:
            if owns:
                await c.aclose()

    return await _retry(_do, what="list_remote_files")


async def download_file(
    api_url: str, token: str, name: str, application: str, client: Optional[httpx.AsyncClient] = None
) -> bytes:
    from urllib.parse import quote
    url = f"{api_url}/api/v1/LogFiles(Name='{quote(name)}',Application='{quote(application)}')/$value"

    async def _do():
        c = client or httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
        owns = client is None
        try:
            resp = await c.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.content
        finally:
            if owns:
                await c.aclose()

    return await _retry(_do, what=f"download_file({name})")
