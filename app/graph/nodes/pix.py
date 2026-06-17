"""Stage 8b — PIX (pix).

Reachable ONLY from `choice` (which only chains here after a preview was
delivered) — that enforces the "PIX only after preview" invariant structurally.
Create a dynamic PIX charge, persist an `orders` row, and send the copia-e-cola
in an isolated bubble for easy copying. Parks at PIX_WAIT. The payment webhook
resolves wa_jid from the order's conversation_id (repo.get_jid_by_conversation).
"""
from __future__ import annotations

import logging

from app.config import settings
from app.graph.nodes import conversation_id, emit_text, get_brief, patch_extra
from app.graph.state import Stage
from app.payments.mercadopago import MercadoPagoProvider
from app.db import repo

logger = logging.getLogger(__name__)


async def pix(state: dict) -> dict:
    conv_id = conversation_id(state)
    jid = state.get("wa_jid") or ""
    external_ref = state.get("order_id") or conv_id or jid or "marina-order"
    price = settings.price_reais

    provider = MercadoPagoProvider()
    charge = await provider.create_pix_charge(
        settings.price_cents, "Musica personalizada", external_ref
    )

    order_id = state.get("order_id")
    if conv_id:
        try:
            # txid holds the real PIX txid when the provider returns one, else
            # None; the webhook resolves wa_jid from conversation_id instead.
            txid = (charge.raw or {}).get("txid") if isinstance(charge.raw, dict) else None
            order = await repo.create_order(
                conv_id, settings.price_cents, charge.payment_id, charge.copia_cola, txid=txid
            )
            if isinstance(order, dict):
                order_id = str(order.get("id") or order_id or "")
        except Exception:  # noqa: BLE001 — never break the send on a DB hiccup
            logger.exception("create_order failed")

    bubbles = [
        f"Pra eu te mandar a musica completa agora e so um pix de R$ {price}:",
        charge.copia_cola,
        "Me manda quando fizer que eu te entrego ela inteirinha 💛",
    ]
    msgs = emit_text(state, bubbles)
    return {
        "mp_payment_id": str(charge.payment_id),
        "order_id": order_id,
        "stage": Stage.PIX_WAIT.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
