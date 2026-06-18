"""Stage 7 — Entrega da prévia (preview).

Resumed by the KIE webhook (runner.on_generation_complete injects `variants`).
Sends the lyric teaser + a ~45s preview, then lands at CHOICE.

Product decision: the preview is a short ~45s VIDEO (the Suno cover art as a
still + the first 45s of the song), built locally with ffmpeg. It's NOT the full
song and NOT view-once (Evolution can't send media view-once) — keeping it short
is what protects the full song as the paid-only deliverable. Falls back to a 45s
audio note if the cover art is missing or the video build fails.
"""
from __future__ import annotations

import asyncio
import logging

from app.graph.nodes import conversation_id, emit_audio, emit_text, emit_video, patch_extra
from app.graph.state import Stage
from app.media import storage
from app.music import kie, lyrics
from app.music import preview as preview_mod
from app.db import repo

logger = logging.getLogger(__name__)


async def preview(state: dict) -> dict:
    variants = state.get("variants") or []
    if not variants:
        # Nothing to preview (failed/empty generation) — leave the wait state.
        return {"outbound": state.get("outbound") or [], "extra": patch_extra(state, _next="end")}

    extra = state.get("extra") or {}
    prompt = extra.get("lyrics_prompt") or ""
    first = variants[0]
    audio_url = first.get("audio_url") or first.get("audioUrl") or ""
    image_url = first.get("image_url") or first.get("imageUrl") or ""
    conv_id = conversation_id(state)
    task_id = state.get("kie_task_id")

    # Lyric teaser (the sales hook).
    partial = lyrics.partial_lyrics(prompt)
    msgs = emit_text(state, ["Olha que linda a letra que saiu 🥹"])
    if partial:
        msgs += emit_text(state, [partial])

    audio_data = await kie.download(audio_url)
    preview_url = None

    # Preferred: a 45s video (cover art + audio). Degrade to audio on any issue.
    if image_url:
        try:
            image_data = await kie.download(image_url)
            clip = await asyncio.to_thread(
                preview_mod.make_video_preview, audio_data, image_data, start=0.0, duration=45.0
            )
            path = storage.build_path(conv_id or state.get("wa_jid", "anon"), prefix="previews", ext="mp4")
            preview_url = await storage.upload(path, clip, "video/mp4")
            msgs += emit_video(state, preview_url, caption="Escuta com calma 💛")
        except Exception:  # noqa: BLE001
            logger.exception("video preview build failed; falling back to audio")
            preview_url = None

    if not preview_url:
        clip = await asyncio.to_thread(preview_mod.make_preview, audio_data, start=0.0, duration=45.0)
        path = storage.build_path(conv_id or state.get("wa_jid", "anon"), prefix="previews", ext="mp3")
        preview_url = await storage.upload(path, clip, "audio/mpeg")
        msgs += emit_audio(state, preview_url, caption="Escuta com calma 💛")

    msgs += emit_text(state, ["Qual parte mexeu mais contigo?"])

    if conv_id and task_id:
        try:
            await repo.update_generation(
                task_id, status="SUCCESS", variants=variants, preview_url=preview_url
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_generation (preview) failed")

    return {
        "preview_url": preview_url,
        "variants": variants,
        "chosen_variant": first.get("id"),
        "stage": Stage.CHOICE.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end", lyrics_prompt=prompt),
    }
