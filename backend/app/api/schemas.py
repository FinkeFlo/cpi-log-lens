"""Request models and input validation shared by the routers."""

import re
from datetime import UTC, date, datetime, time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

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

    @field_validator("date_from", "date_to")
    @classmethod
    def _normalize_date_bound(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_datetime_bound(value)

    @model_validator(mode="after")
    def _check_date_range(self):
        try:
            check_date_range(self.date_from, self.date_to)
        except HTTPException as exc:
            raise ValueError(exc.detail) from None
        return self


class CleanupRequest(BaseModel):
    older_than_days: int = Field(ge=1, le=36500)  # delete entries older than this many days
    tenant: str | None = None  # optional: restrict to one tenant


def normalize_datetime_bound(value: str) -> str:
    """Keep bare dates; normalize offset-aware ISO datetimes to naive UTC.

    Log timestamps are stored as timezone-naive CPI timestamps. Naive API
    datetimes are interpreted as UTC; offset-aware values are normalized to UTC.
    """
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        date.fromisoformat(value)
        return value
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}"
        r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?",
        value,
    ):
        raise ValueError("datetime must use ISO YYYY-MM-DD[THH:MM[:SS[.ffffff]][Z|±HH:MM]]")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        try:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        except OverflowError as exc:
            raise ValueError("datetime offset normalization is outside the supported range") from exc
    return parsed.isoformat(sep=" ")


def check_datetime(value: str | None, field: str) -> str | None:
    """Validate ISO date/datetime input and normalize offset-aware values."""
    if value is None:
        return None
    try:
        return normalize_datetime_bound(value)
    except ValueError:
        raise HTTPException(422, f"{field} must be an ISO date or datetime") from None


def check_date_range(date_from: str | None, date_to: str | None) -> None:
    """Reject inverted ranges; a bare date_to keeps its existing end-of-day meaning."""
    if date_from is None or date_to is None:
        return
    try:
        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to)
        if len(date_to) == 10:
            end = datetime.combine(date.fromisoformat(date_to), time.max)
    except ValueError:
        return  # Individual field validation reports the malformed bound.
    if start > end:
        raise HTTPException(422, "date_from must be earlier than or equal to date_to")
