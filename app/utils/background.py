"""Fire-and-forget task helper.

Webhooks must return 200 immediately and process in the background. `spawn`
schedules a coroutine on the running loop and logs any exception so failures are
never silently swallowed. We keep a strong reference until the task finishes
(asyncio only holds a weak ref otherwise).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable

log = logging.getLogger("marina.bg")
_tasks: set[asyncio.Task] = set()


def spawn(coro: Awaitable, *, name: str = "task") -> asyncio.Task:
    task = asyncio.create_task(_guard(coro, name), name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def _guard(coro: Awaitable, name: str) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001 — log and move on; background work must not crash the app
        log.exception("background task '%s' failed", name)
