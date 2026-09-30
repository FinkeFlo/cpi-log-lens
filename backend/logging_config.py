"""Logging setup: one stdout handler for the app and uvicorn, as text or JSON.

LOG_LEVEL  debug | info | warning | error   (default info)
LOG_FORMAT text | json                      (default text)
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    """One JSON object per line; extra fields passed as `extra={"fields": {...}}`."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "info").upper())
    # Route uvicorn's own messages through the same handler. Its access log is
    # replaced by the request log in main.py, which also records durations.
    for name in ("uvicorn", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers[:] = []
        uv.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
