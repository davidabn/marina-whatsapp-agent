"""Stage 5 — Ancoragem suave (anchor).

The ONLY place price first appears (R$ <price> via settings.price_reais — a code
invariant, not the LLM's call). The how-it-works bubbles are deterministic so
the 1/2/3 framing + the exact amount can never drift.

Two entries, distinguished by the persisted `anchor_explained` flag:
1. Chained from `style` (flag unset): emit the explanation + value anchor +
   "Posso comecar a gerar?", set the flag, END (await consent).
2. A later user turn at ANCHOR (flag set): if the message is affirmative, set
   `generation_consent` and CHAIN to songwriter -> generate; otherwise nudge.
"""
from __future__ import annotations

import re

from app.config import settings
from app.graph.nodes import (
    buyer_adj,
    emit_text,
    get_brief,
    history,
    patch_extra,
    recipient_pronoun,
)
from app.graph.state import Stage
from app.llm import reply

_AFFIRMATIVE = re.compile(
    r"\b(sim+|pode|claro|bora|vamos|quero|manda|isso|ok|okay|aham|uhum|positivo|"
    r"com certeza|fechado|partiu|por favor|pf|simbora|adoro|adorei|amei|gostei)\b",
    re.IGNORECASE,
)


def _is_affirmative(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if _AFFIRMATIVE.search(t):
        return True
    return t in {"👍", "🙌", "❤️", "💛", "s", "ss"}


def _explanation_bubbles(brief) -> list[str]:
    price = settings.price_reais
    name = brief.recipient_name or "essa pessoa"
    tranq = buyer_adj(brief, "tranquilo", "tranquila", "por dentro de tudo")
    rec = recipient_pronoun(brief, unknown=name)
    return [
        f"Deixa eu te explicar rapidinho como funciona pra tu ja ficar {tranq}:",
        "1. Eu vou criar a musica com a historia de voces",
        "2. Te mando alguns segundos de previa pra tu sentir como ficou 💛",
        f"3. Quando tu gostar, e so finalizar o pagamento (R$ {price}) num link "
        f"rapidinho que dai eu te mando a musica completa, com a letra inteira, "
        f"pra voce enviar pra {name}",
        "Pensa que e mais barato que um buque de flores que murcha em 3 dias 🌸 e "
        f"muito mais valioso — e uma musica que so existe pra voces, que {rec} vai "
        "poder escutar pra sempre 💛",
        "Posso comecar a gerar?",
    ]


async def anchor(state: dict) -> dict:
    extra = state.get("extra") or {}
    brief = get_brief(state)

    if not extra.get("anchor_explained"):
        msgs = emit_text(state, _explanation_bubbles(brief))
        return {
            "stage": Stage.ANCHOR.value,
            "outbound": state["outbound"],
            "messages": msgs,
            "extra": patch_extra(state, anchor_explained=True, _next="end"),
        }

    # Consent turn.
    if _is_affirmative(state.get("inbound_text", "")):
        return {
            "generation_consent": True,
            "stage": Stage.SONGWRITER.value,
            "outbound": state["outbound"],
            "extra": patch_extra(state, _next="songwriter"),
        }

    instruction = (
        "A pessoa ainda nao confirmou se pode comecar. Sem pressao, reforce com "
        "carinho que a musica vai ficar linda e pergunte de novo, gentilmente, se "
        "pode comecar a gerar. Curto."
    )
    bubbles = await reply.compose(history(state), instruction, brief=brief)
    msgs = emit_text(state, bubbles)
    return {
        "stage": Stage.ANCHOR.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
