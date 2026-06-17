"""Shared helpers for the graph nodes.

Nodes are pure-ish: they update `brief`/state and APPEND send-actions to
`state["outbound"]` (a list of dicts). The runner — never the node — flushes
those actions to WhatsApp, which keeps nodes free of network-send and unit
testable. This module holds the tiny primitives every node leans on:

- `emit_text` / `emit_audio`: append outbound actions (and return the matching
  chat-history messages so the node can persist Marina's turn).
- `get_brief` / `dump_brief` / `apply_updates`: Brief <-> state dict glue.
- `patch_extra`: merge-update the persisted `extra` bag without clobbering
  sibling keys (LastValue channel => a bare return would wipe the rest).

This package __init__ deliberately imports NO node submodule, so `build.py` /
`router.py` can import the submodules and the submodules can import these
helpers without an import cycle.
"""
from __future__ import annotations

from typing import Any

from app.graph.state import Brief

# Sentinel meaning "delete this key" in patch_extra.
DELETE: Any = object()


# --------------------------------------------------------------------------- #
# Outbound actions
# --------------------------------------------------------------------------- #
def emit_text(state: dict, bubbles: list[str] | str | None) -> list:
    """Append one text action per bubble; return [("ai", bubble), ...].

    The returned list is meant to be merged into the `messages` channel so the
    history reflects what Marina actually said this turn.
    """
    if isinstance(bubbles, str):
        bubbles = [bubbles]
    items = [b for b in (bubbles or []) if b]
    out = list(state.get("outbound") or [])
    out.extend({"kind": "text", "text": b} for b in items)
    state["outbound"] = out
    return [("ai", b) for b in items]


def emit_audio(state: dict, url: str, *, caption: str | None = None) -> list:
    """Append an audio action (preview / full song). Returns [] (no chat text)."""
    if not url:
        return []
    out = list(state.get("outbound") or [])
    item: dict[str, Any] = {"kind": "audio", "url": url}
    if caption:
        item["caption"] = caption
    out.append(item)
    state["outbound"] = out
    return []


# --------------------------------------------------------------------------- #
# Brief glue
# --------------------------------------------------------------------------- #
def get_brief(state: dict) -> Brief:
    return Brief(**(state.get("brief") or {}))


def buyer_adj(brief: Brief, masc: str, fem: str, neutral: str) -> str:
    """Pick a buyer-directed word by the buyer's gender; `neutral` when unknown."""
    return {"m": masc, "f": fem}.get(brief.buyer_gender() or "", neutral)


def recipient_pronoun(brief: Brief, *, unknown: str | None = None) -> str:
    """'ele'/'ela' for the recipient (from the relationship); falls back to
    `unknown` (or the recipient name / 'a pessoa') when gender is unknown."""
    g = brief.recipient_gender()
    if g == "m":
        return "ele"
    if g == "f":
        return "ela"
    return unknown if unknown is not None else (brief.recipient_name or "a pessoa")


def dump_brief(brief: Brief) -> dict:
    """JSON-safe dump (enums -> strings) so it round-trips the checkpointer/DB."""
    return brief.model_dump(mode="json")


def apply_updates(brief: Brief, updates: dict) -> Brief:
    """Merge non-None slot updates into a Brief; union `special_phrases`."""
    data = brief.model_dump()
    for key, value in (updates or {}).items():
        if key == "special_phrases":
            cur = list(data.get("special_phrases") or [])
            for phrase in value or []:
                if phrase not in cur:
                    cur.append(phrase)
            data["special_phrases"] = cur
        else:
            data[key] = value
    return Brief(**data)


# --------------------------------------------------------------------------- #
# extra bag
# --------------------------------------------------------------------------- #
def patch_extra(state: dict, **changes: Any) -> dict:
    """Return a copy of `state['extra']` with `changes` applied.

    Pass `DELETE` as a value to drop a key. Always return this from a node so
    the persisted extra (conversation_id, regen flags, lyrics_prompt, ...) is
    preserved across the LastValue overwrite.
    """
    extra = dict(state.get("extra") or {})
    for key, value in changes.items():
        if value is DELETE:
            extra.pop(key, None)
        else:
            extra[key] = value
    return extra


def history(state: dict) -> list:
    return list(state.get("messages") or [])


def conversation_id(state: dict) -> str | None:
    cid = (state.get("extra") or {}).get("conversation_id")
    return str(cid) if cid else None
