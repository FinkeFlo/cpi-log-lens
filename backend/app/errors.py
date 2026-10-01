"""Error responses: every error is an HTTP status code with a JSON body
`{"detail": ...}` — a message, or for 422 the list of invalid fields."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.repositories.database import DBBusyError
from app.services.fetch import JobAlreadyRunning

log = logging.getLogger("cpi")


async def _db_busy(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "5"})


async def _job_running(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, JobAlreadyRunning)
    return JSONResponse({"detail": "A fetch is already running.", "job_id": exc.job.id}, status_code=409)


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.error("%s %s failed", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


def install(app: FastAPI) -> None:
    app.add_exception_handler(DBBusyError, _db_busy)
    app.add_exception_handler(JobAlreadyRunning, _job_running)
    app.add_exception_handler(Exception, _unexpected)
