"""Stage 4 — Estilo musical (style).

Capture `style_request` (free text, possibly an artist reference). When we have
it: approve warmly and CHAIN into the anchor node (same turn) so Marina explains
how it works right after praising the choice — matching the script.

Doubles as the OBJECTION_STYLE regen path: when the router parks us here with a
regen flag, we re-open the "what didn't fit?" question, and once a new style is
given we CHAIN straight to songwriter -> generate (skipping the anchor, since
consent + preview already happened).
"""
from __future__ import annotations

from app.graph.nodes import (
    DELETE,
    apply_updates,
    dump_brief,
    emit_text,
    get_brief,
    history,
    patch_extra,
)
from app.graph.state import Stage
from app.llm import extract, reply


async def style(state: dict) -> dict:
    extra = state.get("extra") or {}
    regen = bool(extra.get("regen")) and extra.get("regen_kind") == "style"

    brief = get_brief(state)
    updates = await extract.extract_slots(
        history(state), state.get("inbound_text", ""), brief, Stage.STYLE.value
    )
    new_style = "style_request" in updates
    brief = apply_updates(brief, updates)

    # --- OBJECTION_STYLE regen path -------------------------------------- #
    if regen:
        if new_style:
            return {
                "stage": Stage.SONGWRITER.value,
                "brief": dump_brief(brief),
                "outbound": state["outbound"],
                "extra": patch_extra(
                    state, _next="songwriter", _is_regen=True, regen=DELETE, regen_kind=DELETE
                ),
            }
        instruction = (
            "A pessoa nao gostou do estilo da musica. Nunca defenda. Pergunte com "
            "carinho o que nao combinou — era a batida, a voz, o ritmo? — dizendo "
            "que voce ja refaz. Curto."
        )
        bubbles = await reply.compose(history(state), instruction, brief=brief)
        msgs = emit_text(state, bubbles)
        return {
            "stage": Stage.STYLE.value,
            "brief": dump_brief(brief),
            "outbound": state["outbound"],
            "messages": msgs,
            "extra": patch_extra(state, _next="end"),
        }

    # --- normal path ----------------------------------------------------- #
    if brief.has_style():
        instruction = (
            "Aprove a escolha do estilo com carinho ('Adorei a escolha, vai ficar "
            "a cara de voces'). So isso, curtinho — a explicacao de como funciona "
            "vem em seguida."
        )
        bubbles = await reply.compose(history(state), instruction, brief=brief)
        msgs = emit_text(state, bubbles)
        return {
            "stage": Stage.ANCHOR.value,
            "brief": dump_brief(brief),
            "outbound": state["outbound"],
            "messages": msgs,
            "extra": patch_extra(state, _next="anchor"),
        }

    instruction = (
        "Pergunte qual estilo musical combina mais com eles. Ofereca um menu CURTO "
        "(sertanejo, MPB romantica, pop acustica, pagode, gospel, forro) ou peca um "
        "artista de referencia que voce adapta. Nao liste vinte opcoes. Curto."
    )
    bubbles = await reply.compose(history(state), instruction, brief=brief)
    msgs = emit_text(state, bubbles)
    return {
        "stage": Stage.STYLE.value,
        "brief": dump_brief(brief),
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
