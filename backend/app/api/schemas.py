"""Request models and input validation shared by the routers."""

from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

# Tenant ids end up in URLs and directory names: lowercase letters, digits,
# "-" and "_" only. "all" is reserved as the "every tenant" sentinel.
TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
LogType = Literal["trace", "http"]
MAX_HOURS = 24 * 365


class TenantCreate(BaseModel):
    id: str = Field(pattern=TENANT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    api_url: str = Field(pattern=r"^https?://\S+$", max_length=500)
    oauth_url: str = Field(pattern=r"^https?://\S+$", max_length=500)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(max_length=2000)  # empty on update = keep the stored one

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v == "all":
            raise ValueError('"all" is reserved')
        return v


class FetchRequest(BaseModel):
    tenants: list[str] = Field(default=["all"], min_length=1)  # ["all"] or tenant ids
    log_types: list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours: int = Field(default=24, ge=0, le=MAX_HOURS)  # 0 = no filter (all available)


class ScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    tenants: list[str] = Field(default=["all"], min_length=1)
    log_types: list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours: int = Field(default=1, ge=0, le=MAX_HOURS)  # time range pulled on every run
    interval_minutes: int = Field(default=15, ge=5, le=7 * 24 * 60)  # how often to run
    enabled: bool = True


class DefaultFetchConfig(BaseModel):
    tenants: list[str] = Field(default=["all"], min_length=1)
    log_types: list[LogType] = Field(default=["trace", "http"], min_length=1)
    hours: int = Field(default=24, ge=0, le=MAX_HOURS)


class LLMQueryRequest(BaseModel):
    tenant: str | None = None  # tenant id, or null for all
    level: str | None = None  # ERROR | WARN | INFO | DEBUG
    iflow: str | None = None  # partial iflow name match
    grep: str | None = None  # full-text search in message/logger
    date_from: str | None = None  # ISO datetime, e.g. "2024-01-01 00:00:00"
    date_to: str | None = None  # ISO datetime, e.g. "2024-01-31 23:59:59"
    limit: int = 50  # max log entries returned (1–200)


class CleanupRequest(BaseModel):
    older_than_days: int = Field(ge=1, le=36500)  # delete entries older than this many days
    tenant: str | None = None  # optional: restrict to one tenant


def check_datetime(value: str | None, field: str) -> None:
    """date_from/date_to must be an ISO date or datetime; anything else used to
    reach DuckDB and fail there with a 500."""
    if value:
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise HTTPException(422, f"{field} must be YYYY-MM-DD or YYYY-MM-DD HH:MM:SS") from None
