"""Stage 10 — Pos-venda & follow-ups (followup).

Driven by the scheduler (runner.run_due_followups invokes the graph with
event='followup' and extra['followup_kind']). Composes the right warm copy for
the kind:

- postsale: 24-48h after delivery, asks how the reaction went (opens UGC).
- cold_1/2/3: re-engages a lead who went quiet after the pix was requested,
  escalating gently and stopping after the third.
"""
from __future__ import annotations

from app.graph.nodes import DELETE, emit_text, get_brief, history, patch_extra
from app.llm import reply

_INSTRUCTIONS = {
    "postsale": (
        "Ja se passaram 24-48h desde a entrega. Volte com genuino carinho so pra "
        "perguntar como foi a reacao de {name} ao ouvir a musica. Sem vender nada. Curto."
    ),
    "cold_1": (
        "A pessoa ficou quieta depois que voce mandou o link de pagamento (2 a 4 horas atras). "
        "Pergunte com leveza se esta tudo bem e lembre, sem pressao, que a musica de "
        "{name} esta guardada esperando — se quiser que voce mande pra {name} se "
        "emocionar ainda hoje, e so chamar. Curto."
    ),
    "cold_2": (
        "E a manha do dia seguinte. De bom dia com carinho e diga que voce nao para "
        "de pensar na historia de {name}; a musica carrega isso e seria uma pena "
        "{name} nunca chegar a ouvir. Sem pressao dura. Curto."
    ),
    "cold_3": (
        "Ultimo toque (48-72h depois), prometa que e o ultimo. A musica ainda esta "
        "aqui; se mudou de ideia ou ficou pra outro momento, sem stress; mas se quiser "
        "fazer essa surpresa pro {name}, e so dizer. Curto."
    ),
}


async def followup(state: dict) -> dict:
    extra = state.get("extra") or {}
    kind = extra.get("followup_kind") or "postsale"
    brief = get_brief(state)
    name = brief.recipient_name or "ele"
    instruction = _INSTRUCTIONS.get(kind, _INSTRUCTIONS["postsale"]).format(name=name)

    bubbles = await reply.compose(history(state), instruction, brief=brief)
    msgs = emit_text(state, bubbles)
    return {
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end", event=DELETE, followup_kind=DELETE),
    }
