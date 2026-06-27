"""Stage 8b — checkout (node still named `pix` for graph/stage compatibility).

Reachable ONLY from `choice` (which only chains here after a preview was
delivered) — that enforces the "checkout only after preview" invariant
structurally. Creates (or reuses) an InfinitePay checkout link, persists an
`orders` row, and sends the link in an isolated bubble. Parks at PIX_WAIT.

Idempotency: if an unpaid order already exists for this conversation we reuse its
link instead of minting a second checkout. The `order_nsu` we generate is the
join key the webhook matches back (stored in `orders.mp_payment_id`); the
checkout URL is stored in `orders.pix_copia_cola`. No schema change.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from app.config import settings
from app.db import repo
from app.graph.nodes import conversation_id, emit_text, get_brief, patch_extra
from app.graph.state import Stage

logger = logging.getLogger(__name__)


async def pix(state: dict) -> dict:
    # Lazy import avoids a circular import (runner -> build -> nodes).
    from app.graph import runner

    conv_id = conversation_id(state)
    jid = state.get("wa_jid") or ""
    brief = get_brief(state)
    recipient = (brief.recipient_name if brief else None) or "essa pessoa"
    price = settings.price_reais

    order_id = state.get("order_id")
    order_nsu: str | None = None
    checkout_url = ""

    # 1) Reuse an existing unpaid checkout for this conversation, if any.
    if conv_id:
        try:
            existing = await repo.get_pending_order_by_conversation(conv_id)
        except Exception:  # noqa: BLE001
            existing = None
            logger.exception("get_pending_order_by_conversation failed")
        if existing and existing.get("pix_copia_cola"):
            order_nsu = existing.get("mp_payment_id")
            checkout_url = existing.get("pix_copia_cola") or ""
            order_id = str(existing.get("id") or order_id or "")

    # 2) Otherwise mint a fresh order_nsu and create a personalized checkout.
    if not checkout_url:
        order_nsu = uuid4().hex
        try:
            contact = await repo.get_contact(jid) if jid else None
        except Exception:  # noqa: BLE001
            contact = None
        phone = (jid.split("@", 1)[0] if jid else "") or None
        push_name = (contact or {}).get("push_name")
        customer = {"name": push_name or recipient, "phone_number": phone}

        provider = runner.get_payment_provider()
        try:
            charge = await provider.create_pix_charge(
                settings.price_cents,
                f"Musica personalizada para {recipient}",
                order_nsu,
                customer=customer,
            )
            checkout_url = charge.checkout_url or charge.copia_cola
        except Exception:  # noqa: BLE001
            logger.exception("create checkout failed")
            checkout_url = ""

        if conv_id and checkout_url:
            try:
                order = await repo.create_order(
                    conv_id, settings.price_cents, order_nsu, checkout_url,
                    provider="infinitepay",
                )
                if isinstance(order, dict):
                    order_id = str(order.get("id") or order_id or "")
            except Exception:  # noqa: BLE001 — never break the send on a DB hiccup
                logger.exception("create_order failed")

    # 3) Graceful degradation: if the link couldn't be generated, flag for a
    #    human instead of dead-ending the customer at a broken checkout.
    if not checkout_url:
        bubbles = [
            "Deu uma engasgada aqui pra gerar o link de pagamento 😅 ja te resolvo "
            "e te chamo, ta? 💛",
        ]
        msgs = emit_text(state, bubbles)
        return {
            "order_id": order_id,
            "needs_human": True,
            "stage": Stage.PIX_WAIT.value,
            "outbound": state["outbound"],
            "messages": msgs,
            "extra": patch_extra(state, _next="end"),
        }

    bubbles = [
        f"Pra eu te mandar a musica completa do {recipient} agora e so finalizar aqui o:",
        checkout_url,
        "Assim que cair o pagamento eu ja te mando ela completinha 💛",
    ]
    msgs = emit_text(state, bubbles)
    return {
        "mp_payment_id": str(order_nsu),
        "order_id": order_id,
        "stage": Stage.PIX_WAIT.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
