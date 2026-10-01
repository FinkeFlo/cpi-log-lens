"""Network guards and the request log."""

import logging
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings

log = logging.getLogger("cpi")


def install(app: FastAPI, settings: Settings) -> None:
    # Safe defaults for an app that runs on the user's machine without login:
    # - Only requests addressed to an allowed host name are served. This blocks
    #   DNS rebinding, where a web page re-points its own domain at 127.0.0.1.
    # - No CORS by default: the UI is served from the same origin, so other web
    #   pages cannot read API responses. CORS_ORIGINS opts specific origins in.
    # - Writes coming from another origin are rejected (see below), because a
    #   page can still *send* simple cross-site requests without CORS.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.middleware("http")(reject_cross_origin_writes)
    app.middleware("http")(request_log_and_no_cache_js)


async def reject_cross_origin_writes(request: Request, call_next):
    """Browsers attach an Origin header to cross-site POST/PUT/DELETE requests,
    including plain form posts that need no CORS preflight. Such a request
    from any page other than this app's own origin is refused, so a web page
    open in the same browser cannot create tenants, start fetches or clear
    the database. Requests without Origin (curl, scripts, LLM tools) are not
    affected."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if (
            origin
            and origin not in get_settings().cors_origins
            and urlsplit(origin).netloc != request.headers.get("host")
        ):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    return await call_next(request)


async def request_log_and_no_cache_js(request: Request, call_next):
    """Log every request with its duration (replaces uvicorn's access log) and
    prevent browser caching of the frontend scripts."""
    t0 = time.perf_counter()
    response = await call_next(request)
    dur_ms = (time.perf_counter() - t0) * 1000
    level = logging.DEBUG if request.url.path in ("/healthz", "/readyz") else logging.INFO
    if dur_ms > 1000 or response.status_code >= 500:
        level = logging.WARNING
    log.log(
        level,
        "%s %s %s %.0fms",
        request.method,
        request.url.path,
        response.status_code,
        dur_ms,
        extra={
            "fields": {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "dur_ms": round(dur_ms, 1),
            }
        },
    )
    if request.url.path.endswith(".js"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response
