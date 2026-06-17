"""Inbound debounce/coalesce.

WhatsApp users fire several short bubbles in a row. We buffer inbound text per
JID for `debounce_seconds` and process them as ONE LLM turn, so a half-typed
story doesn't prematurely advance the stage.

Usage:
    buf = DebounceBuffer(settings.debounce_seconds)
    text = await buf.collect(jid, incoming_text)   # None if superseded by a newer bubble
    if text is not None:
        ...process the joined text...
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class _Pending:
    parts: list[str] = field(default_factory=list)
    seq: int = 0


class DebounceBuffer:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self._pending: dict[str, _Pending] = {}

    async def collect(self, jid: str, text: str) -> str | None:
        """Append `text`; wait the debounce window. Returns the joined buffer if
        this call is the last one in the window, else None."""
        p = self._pending.setdefault(jid, _Pending())
        p.parts.append(text)
        p.seq += 1
        my_seq = p.seq

        await asyncio.sleep(self.seconds)

        p = self._pending.get(jid)
        if p is None or p.seq != my_seq:
            return None  # a newer bubble arrived; let that call flush
        joined = "\n".join(s for s in p.parts if s).strip()
        self._pending.pop(jid, None)
        return joined
