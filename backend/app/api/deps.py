"""FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from app.repositories.database import Database
from app.services.fetch import FetchService
from app.services.scheduler import ScheduleService


def get_db(request: Request) -> Database:
    """The database opened by the app's lifespan."""
    return request.app.state.db


def get_fetch(request: Request) -> FetchService:
    """The fetch job service created by the app's lifespan."""
    return request.app.state.fetch


def get_schedules(request: Request) -> ScheduleService:
    """The schedule service created by the app's lifespan."""
    return request.app.state.schedules


DbDep = Annotated[Database, Depends(get_db)]
SchedulesDep = Annotated[ScheduleService, Depends(get_schedules)]
FetchDep = Annotated[FetchService, Depends(get_fetch)]
