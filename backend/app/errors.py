"""Mapping of application exceptions to HTTP responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.repositories.database import DBBusyError


async def _db_busy(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "5"})


def install(app: FastAPI) -> None:
    app.add_exception_handler(DBBusyError, _db_busy)
