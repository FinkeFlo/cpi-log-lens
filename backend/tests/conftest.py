import asyncio
import os

# Settings are read when the app modules are imported, so they are set first.
os.environ.update(
    {
        "WATCHDOG_STALL_SECONDS": "0",  # the watchdog thread would kill the test run between event loops
        "TENANTS_CONFIG": "/nonexistent",
        "MOCK": "false",
        "RETENTION_DAYS": "0",
        "LOG_LEVEL": "warning",
    }
)

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app import main
from app.config import Settings, get_settings
from app.cpi import client as cpi_api
from app.repositories import database
from app.services import stats
from tests.support import FakeCpi

BASE_URL = "http://localhost"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _close_app_state():
    # Normally done by the app's shutdown; tests without the app (db fixture) or
    # that stopped a loop themselves clean up here.
    for task in list(main._background_tasks):
        task.cancel()
    await asyncio.gather(*main._background_tasks, return_exceptions=True)
    main._background_tasks.clear()
    await database.close_db()
    stats.invalidate()
    stats._locks.clear()


@pytest.fixture
def settings() -> Settings:
    """The app's settings; change them with monkeypatch.setattr(settings, ...)."""
    return get_settings()


@pytest.fixture
async def app_env(tmp_path, monkeypatch, settings):
    """Fresh database and log directory for one test; the app is not started."""
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.duckdb")
    monkeypatch.setattr(settings, "logs_dir", tmp_path / "logs")
    monkeypatch.setattr(main, "_active_job", None)
    monkeypatch.setattr(cpi_api, "RETRY_BACKOFF_BASE", 0)
    stats.invalidate()
    stats._locks.clear()
    yield tmp_path
    await _close_app_state()


@pytest.fixture
async def client(app_env):
    """HTTP client against the started app (startup ran, DB initialized)."""
    async with (
        LifespanManager(main.app) as manager,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url=BASE_URL) as c,
    ):
        yield c


@pytest.fixture
async def db(app_env):
    """Initialized database without the HTTP app."""
    await database.init_db(get_settings().db_path)
    return await database.get_db()


@pytest.fixture
def fake_cpi(monkeypatch):
    """Fake CPI API; every HTTP client the CPI module creates talks to it."""
    fake = FakeCpi()
    real_client = httpx.AsyncClient

    def client_with_fake_transport(*args, **kwargs):
        kwargs.setdefault("transport", httpx.ASGITransport(app=fake.app))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(cpi_api.httpx, "AsyncClient", client_with_fake_transport)
    return fake
