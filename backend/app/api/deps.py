"""FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from app.repositories.database import Database
from app.services.fetch import FetchService


def get_db(request: Request) -> Database:
    """The database opened by the app's lifespan."""
    return request.app.state.db


def get_fetch(request: Request) -> FetchService:
    """The fetch job service created by the app's lifespan."""
    return request.app.state.fetch


DbDep = Annotated[Database, Depends(get_db)]
FetchDep = Annotated[FetchService, Depends(get_fetch)]
