"""Stage 6 — Geracao (generate).

Submit the stashed payload to KIE, persist a `generations` row, emit the warm
"to gerando" copy, and park at GENERATION_WAIT. The KIE webhook later wakes the
conversation via runner.on_generation_complete -> preview, which resolves the
wa_jid from the generation's conversation_id (repo.get_jid_by_conversation).
"""
from __future__ import annotations

from app.graph.nodes import DELETE, conversation_id, emit_text, patch_extra
from app.graph.state import Stage
from app.music import kie
from app.music.schema import KiePayload
from app.db import repo

_BUBBLES = [
    "Show, vou gerar agora ✨",
    "Demora uns minutinhos pra ficar pronta — to fazendo com calma pra ficar do "
    "jeito que ele merece 💛",
]


async def generate(state: dict) -> dict:
    extra = state.get("extra") or {}
    payload = KiePayload.model_validate(extra.get("kie_payload") or {})
    task_id = await kie.submit(payload)

    conv_id = conversation_id(state)
    if conv_id:
        # Persist the clean KIE payload; the webhook resolves wa_jid from the
        # generation's conversation_id via repo.get_jid_by_conversation.
        await repo.create_generation(conv_id, task_id, payload.to_kie_json())

    is_regen = bool(extra.get("_is_regen"))
    regen_count = int(state.get("regen_count") or 0) + (1 if is_regen else 0)

    msgs = emit_text(state, _BUBBLES)
    return {
        "kie_task_id": task_id,
        "stage": Stage.GENERATION_WAIT.value,
        "regen_count": regen_count,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end", _is_regen=DELETE, kie_payload=DELETE),
    }
