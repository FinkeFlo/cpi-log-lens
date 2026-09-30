"""Test helpers: synthetic CPI log lines and a minimal fake of the CPI LogFiles API."""

import asyncio
import gzip
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app import main
from app.repositories.database import Database


def log_line(
    ts: str = "2026-01-15 08:00:00",
    level: str = "INFO",
    logger: str = "com.example.Logger",
    thread: str = "Camel (Demo_Flow) thread 1 - timer://Demo_Flow",
    message: str = "hello",
    ip: str = "192.0.2.10",
    node: str = "1",
) -> str:
    """One line in the CPI trace format (15 '#'-separated fields)."""
    return f"{ts}#+0000#{level}#{logger}#anonymous#{thread}#com.example.category#na#na#na#na#{message}#-#{ip}#{node}"


def numbered_lines(n: int, start_minute: int = 0, **kw) -> list[str]:
    """n parsable lines with increasing timestamps (one per minute) and messages 'msg <i>'."""
    start = datetime(2026, 1, 15, 8, 0, 0) + timedelta(minutes=start_minute)
    return [
        log_line(ts=(start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), message=f"msg {i}", **kw)
        for i in range(n)
    ]


def write_log(path: Path, lines: list[str], *, gz: bool = False) -> Path:
    data = ("\n".join(lines) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(data) if gz else data)
    return path


@dataclass
class RemoteFile:
    content: bytes
    log_type: str = "trace"
    application: str = "app1"
    size: int | None = None  # announced size; defaults to len(content)
    last_modified_ms: int = field(default_factory=lambda: int(time.time() * 1000))


class FakeCpi:
    """In-process fake of the CPI OAuth token endpoint and the LogFiles OData API.

    Like the real API, $value streams the file decompressed."""

    def __init__(self) -> None:
        self.files: dict[str, RemoteFile] = {}
        self.token_status = 200
        self.list_status = 200
        self.download_status: dict[str, list[int]] = {}  # name -> status per attempt (then 200)
        self.download_delay = 0.0
        self.requests: list[str] = []
        self.app = Starlette(
            routes=[
                Route("/oauth/token", self._token, methods=["POST"]),
                Route("/api/v1/{rest:path}", self._odata, methods=["GET"]),
            ]
        )

    def add(self, name: str, lines: list[str], **kw) -> RemoteFile:
        f = RemoteFile(content=("\n".join(lines) + "\n").encode(), **kw)
        self.files[name] = f
        return f

    async def _token(self, request: Request) -> Response:
        self.requests.append("token")
        if self.token_status != 200:
            return JSONResponse({"error": "unauthorized"}, status_code=self.token_status)
        return JSONResponse({"access_token": "fake-token", "expires_in": 3600})

    async def _odata(self, request: Request) -> Response:
        rest = request.path_params["rest"]
        if rest == "LogFiles":
            self.requests.append("list")
            if self.list_status != 200:
                return JSONResponse({"error": "boom"}, status_code=self.list_status)
            m = re.search(r"LogFileType eq '([^']*)'", request.query_params.get("$filter", ""))
            log_type = m.group(1) if m else None
            results = [
                {
                    "Name": name,
                    "Application": f.application,
                    "LogFileType": f.log_type,
                    "NodeScope": "worker",
                    "Size": f.size if f.size is not None else len(f.content),
                    "LastModified": f"/Date({f.last_modified_ms})/",
                }
                for name, f in self.files.items()
                if log_type is None or f.log_type == log_type
            ]
            return JSONResponse({"d": {"results": results}})
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


FAKE_TENANT = {
    "id": "fake",
    "name": "Fake",
    "api_url": "http://fake-cpi.example",
    "oauth_url": "http://fake-cpi.example/oauth/token",
    "client_id": "client",
    "client_secret": "secret",
}


async def wait_for_job(client: httpx.AsyncClient, max_seconds: float = 20) -> dict:
    """Poll /api/fetch/status until the job is no longer running."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    while True:
        status = (await client.get("/api/fetch/status")).json()
        if status["status"] != "running":
            return status
        if loop.time() > deadline:
            raise AssertionError(f"fetch job still running: {status}")
        await asyncio.sleep(0.02)


async def fetch(client: httpx.AsyncClient, **body) -> dict:
    """Start a fetch job and wait for it to finish; returns the final status."""
    res = await client.post("/api/fetch", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True, res.json()
    return await wait_for_job(client)


def app_db() -> Database:
    """The database of the started app (client fixture)."""
    return main.app.state.db
