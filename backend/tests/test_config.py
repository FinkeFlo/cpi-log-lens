"""Settings from environment variables."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import BACKEND_DIR, Settings

ALL_VARS = [
    "DB_PATH",
    "LOGS_DIR",
    "TENANTS_CONFIG",
    "TENANTS_SEED_MODE",
    "MOCK",
    "FETCH_CONCURRENCY",
    "SCHEDULE_CHECK_SECONDS",
    "RETENTION_DAYS",
    "RETENTION_CHECK_HOURS",
    "ALLOWED_HOSTS",
    "CORS_ORIGINS",
    "DUCKDB_MEMORY_LIMIT",
    "DUCKDB_THREADS",
    "DUCKDB_TEMP_DIR",
    "DUCKDB_CHECKPOINT_THRESHOLD",
    "QUERY_TIMEOUT_S",
    "POOL_ACQUIRE_TIMEOUT_S",
    "WRITE_LOCK_TIMEOUT_S",
    "STATS_CACHE_SECONDS",
    "WATCHDOG_STALL_SECONDS",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "FRONTEND_DIR",
    "APP_VERSION",
]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    s = Settings()
    assert s.model_dump() == {
        "db_path": Path("cpi_logs.duckdb"),
        "logs_dir": Path("logs"),
        "tenants_config": Path("/config/tenants.jsonc"),
        "tenants_seed_mode": "create",
        "mock": False,
        "fetch_concurrency": 4,
        "schedule_check_seconds": 60,
        "retention_days": 0,
        "retention_check_hours": 24,
        "allowed_hosts": ["localhost", "127.0.0.1"],
        "cors_origins": [],
        "duckdb_memory_limit": "1.5GB",
        "duckdb_threads": 4,
        "duckdb_temp_dir": "",
        "duckdb_checkpoint_threshold": "512MB",
        "query_timeout_s": 30,
        "pool_acquire_timeout_s": 10,
        "write_lock_timeout_s": 120,
        "stats_cache_seconds": 30,
        "watchdog_stall_seconds": 120,
        "log_level": "info",
        "log_format": "text",
        "frontend_dir": BACKEND_DIR.parent / "frontend",
        "app_version": "dev",
    }
    assert set(ALL_VARS) == {name.upper() for name in Settings.model_fields}


def test_values_from_the_environment(clean_env):
    clean_env.setenv("DB_PATH", "/data/x.duckdb")
    clean_env.setenv("MOCK", "true")
    clean_env.setenv("ALLOWED_HOSTS", " lens.example , localhost,,")
    clean_env.setenv("CORS_ORIGINS", "https://a.example,https://b.example")
    clean_env.setenv("TENANTS_SEED_MODE", "SYNC")
    clean_env.setenv("LOG_LEVEL", "DEBUG")
    clean_env.setenv("RETENTION_CHECK_HOURS", "0.5")
    s = Settings()
    assert s.db_path == Path("/data/x.duckdb")
    assert s.mock is True
    assert s.allowed_hosts == ["lens.example", "localhost"]
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert s.tenants_seed_mode == "sync"
    assert s.log_level == "debug"
    assert s.retention_check_hours == 0.5


def test_empty_variables_count_as_unset(clean_env):
    clean_env.setenv("FETCH_CONCURRENCY", "")
    clean_env.setenv("ALLOWED_HOSTS", "")
    s = Settings()
    assert s.fetch_concurrency == 4
    assert s.allowed_hosts == ["localhost", "127.0.0.1"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FETCH_CONCURRENCY", "0"),
        ("FETCH_CONCURRENCY", "many"),
        ("RETENTION_DAYS", "-1"),
        ("RETENTION_CHECK_HOURS", "0"),
        ("QUERY_TIMEOUT_S", "0"),
        ("TENANTS_SEED_MODE", "merge"),
        ("LOG_FORMAT", "xml"),
        ("MOCK", "maybe"),
    ],
)
def test_invalid_values_name_the_variable(clean_env, name, value):
    clean_env.setenv(name, value)
    with pytest.raises(ValidationError, match=name.lower()):
        Settings()
