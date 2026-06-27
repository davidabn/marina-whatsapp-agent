"""InfinitePay checkout notification.

InfinitePay POSTs here when a checkout is paid. The body carries our `order_nsu`
plus `transaction_nsu` and `invoice_slug`/`slug`. The notification is UNSIGNED, so
it is treated as an untrusted ping — the runner re-confirms via /payment_check
before delivering. We ack fast (InfinitePay expects ~1s) and do the work in the
background.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from app.graph import runner
from app.utils.background import spawn

log = logging.getLogger("marina.webhook.infinitepay")
router = APIRouter()

# InfinitePay wants a fast 200 with this exact ack shape.
_ACK = {"success": True, "message": None}


def _first(body: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = body.get(k)
        if v not in (None, ""):
            return str(v)
    return None


@router.post("/webhooks/infinitepay")
async def infinitepay_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _ACK
    if not isinstance(body, dict):
        return _ACK

    order_nsu = _first(body, "order_nsu")
    if not order_nsu:
        # not a payment notification we can act on — ack and ignore
        log.info("infinitepay webhook without order_nsu: %s", body)
        return _ACK

    transaction_nsu = _first(body, "transaction_nsu")
    slug = _first(body, "invoice_slug", "slug")

    spawn(
        runner.on_infinitepay_payment(order_nsu, transaction_nsu, slug),
        name=f"ip:{order_nsu}",
    )
    return _ACK
