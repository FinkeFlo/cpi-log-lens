"""In-process fake of the CPI OAuth token endpoint and the LogFiles OData API.

It serves the bundled sample log for the demo tenant and MOCK mode (through
httpx.ASGITransport, so the real client code runs end to end without network) and
any files a test adds. Like the real API, $value streams the file decompressed.
Status codes and a delay can be injected to exercise retries and errors."""

import asyncio
import base64
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.config import BACKEND_DIR

# Base URL the clients of the fake use; it never reaches the network.
FAKE_URL = "http://fake-cpi.invalid"
# Bundled synthetic sample logs (see mock/generate_sample.py).
SAMPLE_DIR = BACKEND_DIR / "mock"


@dataclass
class FakeFile:
    content: bytes
    log_type: str = "trace"
    application: str = "sample"
    size: int | None = None  # announced size; defaults to len(content)
    last_modified_ms: int | None = None  # default: the time of the listing


@dataclass
class FakeCpi:
    files: dict[str, FakeFile] = field(default_factory=dict)
    token_status: int = 200
    # Client ID and secret the token endpoint accepts (401 for others); None: any.
    credentials: tuple[str, str] | None = None
    list_status: int = 200
    # File name -> status per download attempt (200 once the list is used up).
    download_status: dict[str, list[int]] = field(default_factory=dict)
    download_delay: float = 0.0
    # "token", "list" and "download <name>", in the order received.
    requests: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.app = Starlette(
            routes=[
                Route("/oauth/token", self._token, methods=["POST"]),
                Route("/api/v1/{rest:path}", self._odata, methods=["GET"]),
            ]
        )

    def add(self, name: str, lines: list[str], **kw) -> FakeFile:
        f = FakeFile(content=("\n".join(lines) + "\n").encode(), **kw)
        self.files[name] = f
        return f

    async def _token(self, request: Request) -> Response:
        self.requests.append("token")
        if self.token_status != 200:
            return JSONResponse({"error": "unauthorized"}, status_code=self.token_status)
        if self.credentials is not None:
            expected = "Basic " + base64.b64encode(":".join(self.credentials).encode()).decode()
            if request.headers.get("authorization") != expected:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"access_token": "fake-token", "expires_in": 3600})

    async def _odata(self, request: Request) -> Response:
        rest = request.path_params["rest"]
        if rest == "LogFiles":
            return self._list(request)
        m = re.match(r"LogFiles\(Name='([^']*)',Application='([^']*)'\)/\$value$", rest)
        if not m:
            return JSONResponse({"error": "not found"}, status_code=404)
        name = m.group(1)  # Starlette has already percent-decoded the path
        self.requests.append(f"download {name}")
        await asyncio.sleep(self.download_delay)
        statuses = self.download_status.get(name)
        if statuses:
            status = statuses.pop(0)
            if status != 200:
                return JSONResponse({"error": "boom"}, status_code=status)
        f = self.files.get(name)
        if f is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(f.content, media_type="application/octet-stream")

    def _list(self, request: Request) -> Response:
        self.requests.append("list")
        if self.list_status != 200:
            return JSONResponse({"error": "boom"}, status_code=self.list_status)
        m = re.search(r"LogFileType eq '([^']*)'", request.query_params.get("$filter", ""))
        log_type = m.group(1) if m else None
        now_ms = int(time.time() * 1000)
        results = [
            {
                "Name": name,
                "Application": f.application,
                "LogFileType": f.log_type,
                "NodeScope": "worker",
                "Size": f.size if f.size is not None else len(f.content),
                "LastModified": f"/Date({f.last_modified_ms if f.last_modified_ms is not None else now_ms})/",
            }
            for name, f in self.files.items()
            if log_type is None or f.log_type == log_type
        ]
        return JSONResponse({"d": {"results": results}})


@lru_cache
def sample_cpi() -> FakeCpi:
    """The fake with the bundled sample logs, as trace log files."""
    fake = FakeCpi()
    for path in sorted(SAMPLE_DIR.glob("*.log")):
        fake.files[path.name] = FakeFile(content=path.read_bytes())
    return fake
