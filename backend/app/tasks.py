"""Background tasks of the app process."""

import asyncio
from collections.abc import Coroutine
from typing import Any

# The event loop keeps only weak references to tasks; a task nobody else
# references can be garbage-collected before it finishes.
background_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Run `coro` in the background, referenced until it finishes."""
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


async def cancel_all() -> None:
    for task in list(background_tasks):
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
