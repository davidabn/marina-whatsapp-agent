"""Stage 7b — Envio do vídeo da prévia (video_ready).

Resumed by the MP4 callback (or the safety poller) via
runner.on_video_complete, which injects `preview_video_url`. Downloads the KIE
music-video, re-hosts it on Supabase Storage and sends it as a WhatsApp
view-once message (visualização única) — the full song, watchable once. Then it
advances to CHOICE.

Idempotent: if a preview was already delivered (preview_url set), it no-ops, so
the callback and the poller can both fire without sending the video twice.
"""
from __future__ import annotations

import logging

from app.graph.nodes import conversation_id, emit_text, emit_video, patch_extra
from app.graph.state import Stage
from app.media import storage
from app.music import kie
from app.db import repo

logger = logging.getLogger(__name__)


async def video_ready(state: dict) -> dict:
    # Already delivered? (duplicate callback/poller) -> do nothing.
    if state.get("preview_url"):
        return {"outbound": state.get("outbound") or [], "extra": patch_extra(state, _next="end")}

    video_url = state.get("preview_video_url") or (state.get("extra") or {}).get("preview_video_url")
    if not video_url:
        return {"outbound": state.get("outbound") or [], "extra": patch_extra(state, _next="end")}

    conv_id = conversation_id(state)
    data = await kie.download(video_url)
    path = storage.build_path(conv_id or state.get("wa_jid", "anon"), prefix="previews", ext="mp4")
    hosted = await storage.upload(path, data, "video/mp4")

    msgs = emit_video(
        state, hosted,
        caption="Escuta com calma 💛 (da pra ver uma vez só, depois some 👀)",
        view_once=True,
    )
    msgs += emit_text(state, ["Qual parte mexeu mais contigo?"])

    task_id = state.get("kie_task_id")
    if conv_id and task_id:
        try:
            await repo.update_generation(task_id, status="SUCCESS", preview_url=hosted)
        except Exception:  # noqa: BLE001
            logger.exception("update_generation (video preview) failed")

    return {
        "preview_url": hosted,
        "stage": Stage.CHOICE.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
