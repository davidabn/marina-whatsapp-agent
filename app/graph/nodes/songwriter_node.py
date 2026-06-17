"""Internal stage — Songwriter (Brief -> KIE payload).

No user I/O. Resolves the free-text style into a name-free descriptor, writes
the song, sanitizes any artist name that slipped through, and stashes the
camelCase payload (+ its lyrics prompt) in `extra` for the generate node. Always
chains to `generate`.
"""
from __future__ import annotations

from app.config import settings
from app.graph.nodes import get_brief, patch_extra
from app.llm import songwriter as songwriter_llm
from app.music import styles


async def songwriter_node(state: dict) -> dict:
    brief = get_brief(state)
    style_resolved = styles.resolve_style(brief.style_request or "")
    payload = await songwriter_llm.write_song(
        brief, style_resolved, call_back_url=settings.kie_callback_url or None
    )
    payload = styles.sanitize_payload(payload)
    return {
        "outbound": state.get("outbound") or [],
        "extra": patch_extra(
            state,
            _next="generate",
            kie_payload=payload.to_kie_json(),
            lyrics_prompt=payload.prompt,
        ),
    }
