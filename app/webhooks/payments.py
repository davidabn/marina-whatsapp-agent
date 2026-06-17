"""Mercado Pago payment notification webhook.

Verifies the x-signature, extracts the payment id, and resumes the graph
(deliver node). The runner re-confirms `approved` via the API before delivering,
so a spoofed notification cannot trigger delivery.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.graph import runner
from app.utils.background import spawn

log = logging.getLogger("marina.webhook.payments")
router = APIRouter()


@router.post("/webhooks/payments")
async def payments_webhook(request: Request):
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    provider = runner.get_mp()

    if not await provider.verify_webhook(headers, raw):
        log.warning("payments webhook failed signature check")
        return Response(status_code=401)

    payment_id = await provider.parse_webhook(headers, body)
    if not payment_id:
        # Non-payment notifications (e.g. merchant_order) — ack and ignore.
        return {"ok": True, "skipped": True}

    spawn(runner.on_payment_event(payment_id), name=f"pay:{payment_id}")
    return {"ok": True}
