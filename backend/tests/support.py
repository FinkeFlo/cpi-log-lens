"""Test helpers: synthetic CPI log lines and a minimal fake of the CPI LogFiles API."""

import asyncio
import gzip
from datetime import datetime, timedelta
from pathlib import Path

import httpx

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
