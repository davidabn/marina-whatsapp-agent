"""Stage 1 — Acolhimento (welcome).

First reply to a brand-new lead: a warm greeting + the only question that
matters here — *who* is the song for. We never ask about the lead themselves.
Advances to DISCOVERY_RECIPIENT.
"""
from __future__ import annotations

from app.graph.nodes import emit_text, get_brief, history, patch_extra
from app.graph.state import Stage
from app.llm import reply

_INSTRUCTION = (
    "Esta e a PRIMEIRA resposta para um lead que acabou de chegar. "
    "Cumprimente com carinho (algo como 'Oii! Que bom te ver aqui') e, em outra "
    "bolha, pergunte com curiosidade genuina pra QUEM e a musica especial (a "
    "pessoa que vai ser presenteada). Nao pergunte nada sobre o cliente (nome, "
    "idade, cidade) — so sobre a pessoa amada. Mensagens curtas."
)


async def welcome(state: dict) -> dict:
    bubbles = await reply.compose(history(state), _INSTRUCTION, brief=get_brief(state))
    msgs = emit_text(state, bubbles)
    return {
        "stage": Stage.DISCOVERY_RECIPIENT.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
