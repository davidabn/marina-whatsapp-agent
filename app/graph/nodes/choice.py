"""Stage 8 — Escolha + ativacao do desejo (choice).

The highest-conversion moment: the customer just reacted to the preview. Mirror
the emotion and PROJECT the recipient's reaction ("imagina a cara do <nome>
ouvindo"), then CHAIN to the pix node (which emits the charge). Pure objections
(too expensive / style / lyrics) are caught upstream by the router, so a normal
turn here is treated as a positive reaction.

Invariant: pix is only reachable from here (and only after preview_url exists).
"""
from __future__ import annotations

from app.graph.nodes import emit_text, get_brief, history, patch_extra
from app.graph.state import Stage
from app.llm import reply


async def choice(state: dict) -> dict:
    brief = get_brief(state)
    name = brief.recipient_name or "essa pessoa"

    # Safety belt for the PIX-after-preview invariant: never advance to pix
    # without a delivered preview.
    if not state.get("preview_url"):
        return {"outbound": state.get("outbound") or [], "extra": patch_extra(state, _next="end")}

    instruction = (
        f"A pessoa reagiu a previa da musica. Reaja junto, com carinho, confirmando "
        f"a emocao ('ficou com a cara de voces'). Depois, em outra bolha, projete a "
        f"reacao: 'imagina a cara de {name} escutando isso'. Nao fale de preco nem de "
        f"pagamento — isso vem logo depois. Curto."
    )
    bubbles = await reply.compose(history(state), instruction, brief=brief)
    msgs = emit_text(state, bubbles)
    return {
        "stage": Stage.PIX.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="pix"),
    }
