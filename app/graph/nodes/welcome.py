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
    "bolha, faca UMA pergunta direta: o NOME da pessoa que vai receber a musica "
    "(ex.: 'qual o nome da pessoa que vai receber essa musica?'). "
    "NAO faca duas perguntas que parecam a mesma coisa — nao pergunte 'pra quem e' "
    "E 'qual o nome' juntos; basta pedir o nome. NAO pergunte ainda a relacao "
    "(isso vem depois) nem nada sobre o cliente (nome dele, idade, cidade). "
    "Mensagens curtas."
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
