"""CPI API client — OAuth2 + Log file fetching."""

import asyncio
import gzip
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger("cpi.client")

# Per-download-request timeouts and retry-with-backoff for the CPI LogFiles
# API: individual file downloads have been observed taking 30-90s (the server
# decompresses the file before streaming it), so timeouts must be generous
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
    last_exc: Exception | None = None
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
            delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            log.warning(
                "%s failed (%s), retrying in %.0fs (attempt %d of %d)",
                what,
                type(last_exc).__name__,
                delay,
                attempt + 1,
                MAX_RETRIES,
            )
            await asyncio.sleep(delay)
    raise last_exc


async def get_token(oauth_url: str, client_id: str, client_secret: str, client: httpx.AsyncClient | None = None) -> str:
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
    api_url: str, token: str, log_type: str, client: httpx.AsyncClient | None = None
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


async def download_to_file(
    api_url: str,
    token: str,
    name: str,
    application: str,
    client: httpx.AsyncClient,
    dest: Path,
) -> int:
    """Stream a log file to `dest` as gzip, chunk by chunk, and return the
    number of bytes received.

    CPI's LogFiles $value endpoint decompresses server-side before streaming,
    even though the name and Content-Type say .gz — the body is 30-40x larger
    than the announced Size. Holding it in memory (resp.content) and then
    compressing a second copy was the main reason fetches got the app
    OOM-killed. Here memory stays constant: each chunk is compressed and
    written in a worker thread, into `<dest>.part`, which replaces `dest`
    only once the download is complete."""
    url = f"{api_url}/api/v1/LogFiles(Name='{quote(name)}',Application='{quote(application)}')/$value"
    part = dest.with_name(dest.name + ".part")

    async def _do():
        received = 0
        async with client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as resp:
            resp.raise_for_status()
            with await asyncio.to_thread(part.open, "wb") as raw:
                gz: gzip.GzipFile | None = None
                try:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        if received == 0 and not chunk.startswith(b"\x1f\x8b"):
                            gz = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6)
                        received += len(chunk)
                        await asyncio.to_thread((gz or raw).write, chunk)
                finally:
                    if gz is not None:
                        await asyncio.to_thread(gz.close)
        os.replace(part, dest)
        return received

    return await _retry(_do, what=f"download_to_file({name})")
