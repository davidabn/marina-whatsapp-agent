"""Stage 7 — Geração do vídeo da prévia (preview).

Resumed by the KIE webhook (runner.on_generation_complete injects `variants`).
The product decision: the prévia is the FULL song as a KIE "music video" (MP4
visualizer), sent in WhatsApp view-once — the customer watches it once (can't
save/forward), then only gets a keepable file after paying.

This node sends the lyric teaser, kicks off the MP4 job (POST /api/v1/mp4/generate)
and parks at GENERATION_WAIT. When the video is ready, the second webhook (or the
safety poller) resumes the graph at the `video_ready` node, which sends the
view-once video and advances to CHOICE.

Fallbacks so the funnel never dead-ends:
- if the MP4 job can't be STARTED, we send the classic 45s audio preview now;
- if it starts but never finishes (poller times out), the runner resumes here
  with `force_audio` and we send the audio preview instead.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.graph.nodes import DELETE, conversation_id, emit_audio, emit_text, patch_extra
from app.graph.state import Stage
from app.media import storage
from app.music import kie, lyrics
from app.music import preview as preview_mod
from app.db import repo

logger = logging.getLogger(__name__)


def _mp4_callback_url(conv_id: str | None) -> str:
    """Callback that carries the conversation id so the webhook can resume it."""
    base = (settings.public_base_url or "").rstrip("/")
    if base and conv_id:
        return f"{base}/webhooks/kie?mp4_conv={conv_id}"
    return settings.kie_callback_url


async def _audio_preview(state: dict, *, first: dict, prompt: str, send_teaser: bool) -> dict:
    """Classic 45s audio preview (fallback path). Lands at CHOICE."""
    conv_id = conversation_id(state)
    task_id = state.get("kie_task_id")
    audio_url = first.get("audio_url") or first.get("audioUrl") or ""

    msgs = []
    if send_teaser:
        partial = lyrics.partial_lyrics(prompt)
        msgs += emit_text(state, ["Olha que linda a letra que saiu 🥹"])
        if partial:
            msgs += emit_text(state, [partial])

    data = await kie.download(audio_url)
    clip = await asyncio.to_thread(preview_mod.make_preview, data, start=0.0, duration=45.0)
    path = storage.build_path(conv_id or state.get("wa_jid", "anon"), prefix="previews", ext="mp3")
    preview_url = await storage.upload(path, clip, "audio/mpeg")

    msgs += emit_audio(state, preview_url, caption="Escuta com calma 💛")
    msgs += emit_text(state, ["Agora escuta com calma 💛", "Qual parte mexeu mais contigo?"])

    if conv_id and task_id:
        try:
            await repo.update_generation(
                task_id, status="SUCCESS", variants=state.get("variants") or [], preview_url=preview_url
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_generation (audio preview) failed")

    return {
        "preview_url": preview_url,
        "chosen_variant": first.get("id"),
        "stage": Stage.CHOICE.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end", force_audio=DELETE, lyrics_prompt=prompt),
    }


async def preview(state: dict) -> dict:
    variants = state.get("variants") or []
    if not variants:
        # Nothing to preview (failed/empty generation) — leave the wait state.
        return {"outbound": state.get("outbound") or [], "extra": patch_extra(state, _next="end")}

    extra = state.get("extra") or {}
    prompt = extra.get("lyrics_prompt") or ""
    force_audio = bool(extra.get("force_audio"))
    first = variants[0]
    conv_id = conversation_id(state)
    task_id = state.get("kie_task_id")

    # Video already arrived (callback won the race)? Nothing to do.
    if force_audio and state.get("preview_url"):
        return {"outbound": state.get("outbound") or [], "extra": patch_extra(state, _next="end", force_audio=DELETE)}

    # Timeout fallback: send the audio preview (teaser was already sent earlier).
    if force_audio:
        return await _audio_preview(state, first=first, prompt=prompt, send_teaser=False)

    # Normal path: lyric teaser, then kick off the music-video.
    partial = lyrics.partial_lyrics(prompt)
    msgs = emit_text(state, ["Olha que linda a letra que saiu 🥹"])
    if partial:
        msgs += emit_text(state, [partial])

    mp4_task = None
    try:
        mp4_task = await kie.submit_mp4(
            str(task_id or ""), str(first.get("id") or ""),
            call_back_url=_mp4_callback_url(conv_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception("mp4 submit failed; falling back to audio preview")

    if not mp4_task:
        # Couldn't start the video — degrade to audio now (teaser already queued).
        return await _audio_preview(state, first=first, prompt=prompt, send_teaser=False)

    msgs += emit_text(
        state, ["To finalizando o video da tua musica 🎬 ja ja te mando aqui 💛"]
    )
    if conv_id and task_id:
        try:
            await repo.update_generation(task_id, status="SUCCESS", variants=variants)
        except Exception:  # noqa: BLE001
            logger.exception("update_generation (preview) failed")

    return {
        "variants": variants,
        "chosen_variant": first.get("id"),
        "mp4_task_id": mp4_task,
        "stage": Stage.GENERATION_WAIT.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end", lyrics_prompt=prompt),
    }
