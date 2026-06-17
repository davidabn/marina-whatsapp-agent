"""Inbound WhatsApp webhook (Evolution API `messages.upsert`).

Returns 200 immediately and processes the message in the background so Evolution
never retries on slow LLM/music work.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.config import settings
from app.evolution.parser import parse_upsert
from app.graph import runner
from app.utils.background import spawn

log = logging.getLogger("marina.webhook.evolution")
router = APIRouter()


def _authorized(request: Request) -> bool:
    """Optional shared-secret check. If EVOLUTION_WEBHOOK_TOKEN is unset, allow
    (dev). If set, require it in the `x-webhook-token` header or `?token=`."""
    expected = settings.evolution_webhook_token
    if not expected:
        return True
    got = request.headers.get("x-webhook-token") or request.query_params.get("token")
    return got == expected


@router.post("/webhooks/evolution")
async def evolution_webhook(request: Request):
    if not _authorized(request):
        return {"ok": False, "error": "unauthorized"}

    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": True, "skipped": "no json"}

    inbound = parse_upsert(event)
    if inbound is None or not inbound.is_actionable:
        return {"ok": True, "skipped": True}

    spawn(runner.handle_inbound(inbound), name=f"inbound:{inbound.phone}")
    return {"ok": True}
