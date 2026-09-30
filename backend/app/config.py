"""Configuration: every setting comes from an environment variable of the same name
in upper case (e.g. ``db_path`` from ``DB_PATH``); all of them are optional.

Invalid values stop the app at start with a message naming the variable. Empty
variables count as unset. The README lists the variables for users."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _comma_list(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


CommaList = Annotated[list[str], NoDecode, BeforeValidator(_comma_list)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    # ── Storage ──
    db_path: Path = Path("cpi_logs.duckdb")
    logs_dir: Path = Path("logs")

    # ── Tenants ──
    tenants_config: Path = Path("/config/tenants.jsonc")
    # create: only add missing tenants (UI edits stay); sync: the file overwrites stored tenants
    tenants_seed_mode: Annotated[Literal["create", "sync"], BeforeValidator(_lower)] = "create"

    # ── Fetching ──
    # Import the bundled sample logs for every tenant instead of calling CPI.
    mock: bool = False
    # Parallel file downloads per tenant and log type. CPI's LogFiles $value
    # endpoint takes ~30-90 s per file (it decompresses server-side), so
    # parallelism is the main client-side lever.
    fetch_concurrency: int = Field(4, ge=1, le=32)
    schedule_check_seconds: int = Field(60, ge=1)
    # Delete entries older than this many days automatically (0 = keep everything).
    retention_days: int = Field(0, ge=0)
    retention_check_hours: float = Field(24, gt=0)

    # ── Network ──
    # Host names the app answers to ("*" disables the check); blocks DNS rebinding.
    allowed_hosts: CommaList = ["localhost", "127.0.0.1"]
    # Extra browser origins that may call the API (the UI itself needs none).
    cors_origins: CommaList = []

    # ── Database engine ──
    # DuckDB would otherwise take 80% of the RAM it sees (inside Docker: the
    # whole VM) and one thread per CPU; keep it inside an explicit budget.
    duckdb_memory_limit: str = "1.5GB"
    duckdb_threads: int = Field(4, ge=1)
    duckdb_temp_dir: str = ""  # "" = DuckDB default (<db file>.tmp)
    # The default (16MB) checkpoints after nearly every import batch, which
    # made bulk imports 2.5-4x slower (measured).
    duckdb_checkpoint_threshold: str = "512MB"
    query_timeout_s: float = Field(30, gt=0)
    pool_acquire_timeout_s: float = Field(10, gt=0)
    write_lock_timeout_s: float = Field(120, gt=0)
    stats_cache_seconds: float = Field(30, ge=0)

    # ── Operations ──
    # Exit (and let Docker restart the app) if the event loop is blocked this long; 0 disables.
    watchdog_stall_seconds: float = Field(120, ge=0)
    log_level: Annotated[Literal["debug", "info", "warning", "error", "critical"], BeforeValidator(_lower)] = "info"
    log_format: Annotated[Literal["text", "json"], BeforeValidator(_lower)] = "text"
    frontend_dir: Path = BACKEND_DIR.parent / "frontend"
    # Set at image build time (Dockerfile ARG VERSION).
    app_version: str = "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
