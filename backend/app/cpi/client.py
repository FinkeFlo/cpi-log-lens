"""CPI API client: OAuth2 client-credentials token, log file list, streaming download."""

import asyncio
import gzip
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self, TypeVar
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
# Answers worth retrying: rate limiting and transient server errors. Client
# errors (auth, not found, bad request) would fail again.
RETRY_STATUS = (429, 500, 502, 503, 504)

T = TypeVar("T")


def epoch_ms(date_str: str) -> int:
    """Extract epoch ms from OData /Date(1786543619000)/ format."""
    m = re.search(r"\d+", date_str or "")
    return int(m.group()) if m else 0


@dataclass(frozen=True)
class RemoteLogFile:
    """One entry of the LogFiles list."""

    name: str
    application: str
    size: int
    last_modified_ms: int

    @classmethod
    def from_odata(cls, entry: dict) -> "RemoteLogFile":
        return cls(
            name=entry.get("Name") or "",
            application=entry.get("Application") or "",
            size=int(entry.get("Size") or 0),
            last_modified_ms=epoch_ms(entry.get("LastModified", "")),
        )


class CpiClient:
    """Client for one CPI tenant. One instance is used for a whole fetch of the
    tenant, so the token call, the file list and every download share one
    connection pool (keep-alive instead of a TCP/TLS handshake per request).

    `transport` replaces the network, e.g. with httpx.ASGITransport for the fake
    CPI server (demo data, MOCK mode, tests)."""

    def __init__(
        self,
        api_url: str,
        oauth_url: str,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff: float | None = None,
        retries: int = MAX_RETRIES,
        timeout: httpx.Timeout | float = DOWNLOAD_TIMEOUT,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.oauth_url = oauth_url
        self._credentials = (client_id, client_secret)
        self._retry_backoff = retry_backoff
        self._retries = retries
        self._token: str | None = None
        self._http = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_token(self, *, follow_redirects: bool = True) -> str:
        """Request a new OAuth token (client credentials); it is used by the following calls."""

        async def _do() -> str:
            resp = await self._http.post(
                self.oauth_url,
                params={"grant_type": "client_credentials"},
                auth=self._credentials,
                follow_redirects=follow_redirects,
            )
            resp.raise_for_status()
            token: str = resp.json()["access_token"]
            return token

        self._token = await self._retry(_do, what="get_token")
        return self._token

    def _auth_header(self) -> dict[str, str]:
        if self._token is None:
            raise RuntimeError("get_token() must be called first")
        return {"Authorization": f"Bearer {self._token}"}

    async def list_files(self, log_type: str) -> list[RemoteLogFile]:
        filter_str = f"LogFileType eq '{log_type}' and NodeScope eq 'worker'"

        async def _do() -> list[RemoteLogFile]:
            resp = await self._http.get(
                f"{self.api_url}/api/v1/LogFiles",
                params={"$filter": filter_str},
                headers={**self._auth_header(), "Accept": "application/json"},
            )
            resp.raise_for_status()
            return [RemoteLogFile.from_odata(e) for e in resp.json().get("d", {}).get("results", [])]

        return await self._retry(_do, what="list_remote_files")

    async def download(self, file: RemoteLogFile, dest: Path) -> int:
        """Stream a log file to `dest` as gzip, chunk by chunk, and return the
        number of bytes received.

        CPI's LogFiles $value endpoint decompresses server-side before streaming,
        even though the name and Content-Type say .gz — the body is 30-40x larger
        than the announced Size. Holding it in memory (resp.content) and then
        compressing a second copy was the main reason fetches got the app
        OOM-killed. Here memory stays constant: each chunk is compressed and
        written in a worker thread, into `<dest>.part`, which replaces `dest`
        only once the download is complete."""
        url = (
            f"{self.api_url}/api/v1/LogFiles(Name='{quote(file.name)}',Application='{quote(file.application)}')/$value"
        )
        part = dest.with_name(dest.name + ".part")

        async def _do() -> int:
            received = 0
            async with self._http.stream("GET", url, headers=self._auth_header()) as resp:
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

        return await self._retry(_do, what=f"download_to_file({file.name})")

    async def _retry(self, fn: Callable[[], Awaitable[T]], *, what: str) -> T:
        """Run a CPI call, retrying transient failures with exponential backoff
        (`retries` attempts in all). Raises the last exception if all attempts fail."""
        backoff = RETRY_BACKOFF_BASE if self._retry_backoff is None else self._retry_backoff
        last_exc: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                return await fn()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in RETRY_STATUS:
                    raise
                last_exc = e
            if attempt < self._retries:
                delay = backoff * (2 ** (attempt - 1))
                log.warning(
                    "%s failed (%s), retrying in %.0fs (attempt %d of %d)",
                    what,
                    type(last_exc).__name__,
                    delay,
                    attempt + 1,
                    self._retries,
                )
                await asyncio.sleep(delay)
        # Every failed attempt sets last_exc; with retries < 1 nothing was tried.
        if last_exc is None:
            raise RuntimeError(f"{what}: no attempt made (retries={self._retries})")
        raise last_exc
