"""Per-contact async serialization.

Two inbound messages for the same WhatsApp contact must never mutate graph state
concurrently. `contact_lock(jid)` yields an asyncio.Lock unique to that JID.
(Single-process assumption — for multi-worker scale, swap for a Redis lock.)
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

_locks: dict[str, asyncio.Lock] = {}
_guard = asyncio.Lock()


async def _get_lock(jid: str) -> asyncio.Lock:
    async with _guard:
        lock = _locks.get(jid)
        if lock is None:
            lock = asyncio.Lock()
            _locks[jid] = lock
        return lock


@asynccontextmanager
async def contact_lock(jid: str):
    lock = await _get_lock(jid)
    async with lock:
        yield
