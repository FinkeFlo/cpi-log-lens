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

from app import cpi, main, tasks
from app.config import Settings, get_settings
from app.cpi import client as cpi_api
from app.cpi.client import CpiClient
from app.cpi.fake import FakeCpi
from app.repositories.database import Database
from app.services import stats

BASE_URL = "http://localhost"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _close_app_state():
    # Normally done by the app's shutdown; tests without the app (db fixture) or
    # that stopped a loop themselves clean up here.
    await tasks.cancel_all()
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
    database = Database.open(get_settings().db_path)
    yield database
    await database.close()


@pytest.fixture
def fake_cpi(monkeypatch):
    """Fake CPI API; CPI requests for regular tenants go to it (the demo tenant and
    MOCK mode keep using the app's own fake with the sample logs)."""
    fake = FakeCpi()
    real_client_for = cpi.client_for

    def client_for(tenant, *, timeout=None):
        if get_settings().mock or cpi.is_demo(tenant):
            return real_client_for(tenant, timeout=timeout)
        transport = httpx.ASGITransport(app=fake.app)
        return CpiClient(
            tenant["api_url"],
            tenant["oauth_url"],
            tenant["client_id"],
            tenant["client_secret"],
            transport=transport,
            retry_backoff=0,
        )

    monkeypatch.setattr(cpi, "client_for", client_for)
    return fake
