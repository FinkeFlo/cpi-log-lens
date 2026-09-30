"""Event-loop liveness: a heartbeat task and a watchdog thread that restarts a hung process."""

import asyncio
import faulthandler
import logging
import os
import threading
import time

log = logging.getLogger("cpi")

_heartbeat_at = time.monotonic()


async def heartbeat() -> None:
    global _heartbeat_at
    while True:
        _heartbeat_at = time.monotonic()
        await asyncio.sleep(1)


def loop_lag() -> float:
    """Seconds since the event loop last ran the heartbeat."""
    return time.monotonic() - _heartbeat_at


def watchdog(stop: threading.Event, stall_seconds: float) -> None:
    """Runs in its own thread. A blocked event loop means every request hangs
    while the process looks alive, so no restart policy would kick in. After
    `stall_seconds` without a heartbeat, dump all stacks (for the post-mortem)
    and exit hard; Docker's restart policy brings the app back. Ends when
    `stop` is set (shutdown)."""
    while not stop.wait(5):
        stalled = loop_lag()
        if stalled > stall_seconds:
            log.critical("event loop stalled for %.0fs, dumping stacks and exiting", stalled)
            faulthandler.dump_traceback(all_threads=True)
            os._exit(70)
