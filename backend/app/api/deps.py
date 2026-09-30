"""FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from app.repositories.database import Database


def get_db(request: Request) -> Database:
    """The database opened by the app's lifespan."""
    return request.app.state.db


DbDep = Annotated[Database, Depends(get_db)]
