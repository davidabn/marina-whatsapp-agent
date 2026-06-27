"""Stage 9 — Entrega completa (deliver).

Resumed by the payment webhook (runner._deliver_for_order, after the payment is
re-confirmed). Sends an immediate "preparando" ack, then hands the heavy work to
a background task (runner.render_and_send_full_video): the paid full song goes
out as Suno's official MP4 visualizer (KIE renders it ~2 min, off our server),
with a local 9:16 ffmpeg video — and finally a full-audio note — as fallbacks.
The celebration / lyrics-offer / UGC-seed bubbles are sent by that task AFTER the
video so they never arrive before it. Schedules the +24h post-sale follow-up and
lands at DONE.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.graph.nodes import (
    conversation_id,
    emit_text,
    get_brief,
    patch_extra,
)
from app.graph.state import Stage
from app.utils.background import spawn
from app.db import repo

logger = logging.getLogger(__name__)


async def deliver(state: dict) -> dict:
    brief = get_brief(state)
    variants = state.get("variants") or []
    chosen_id = state.get("chosen_variant")
    chosen = next(
        (v for v in variants if v.get("id") == chosen_id),
        variants[0] if variants else None,
    )

    conv_id = conversation_id(state)
    jid = state.get("wa_jid") or ""
    task_id = state.get("kie_task_id")
    lyrics_prompt = (state.get("extra") or {}).get("lyrics_prompt") or ""

    # Immediate ack. The full video render (~2 min via the Suno visualizer) and
    # all the post-delivery bubbles happen in the background so the webhook turn
    # stays snappy and nothing arrives before the video.
    msgs = emit_text(state, ["Recebido 💛 deixa eu preparar ela aqui que ja ja te mando 🎶"])

    if chosen:
        # Lazy import avoids a circular import (runner -> build -> nodes).
        from app.graph import runner
        spawn(
            runner.render_and_send_full_video(
                jid, conv_id, chosen, task_id, brief.model_dump(mode="json"), lyrics_prompt
            ),
            name=f"fullvideo:{conv_id or jid}",
        )
    else:
        logger.warning("deliver: no chosen variant for conv %s", conv_id)

    # Cheap bookkeeping stays inline; the full_url is stamped by the bg task.
    if conv_id:
        try:
            run_at = datetime.now(timezone.utc) + timedelta(hours=24)
            await repo.schedule_followup(conv_id, "postsale", run_at)
        except Exception:  # noqa: BLE001
            logger.exception("schedule postsale followup failed")

    return {
        "paid": True,
        "stage": Stage.DONE.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
