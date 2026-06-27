"""Stage 9 — Entrega completa (deliver).

Resumed by the payment webhook (runner._deliver_for_order, after the payment is
re-confirmed). Builds the FULL song as a vertical 9:16 video (Suno cover art +
the whole track), uploads it, then sends the video + the UGC seed ("grava a
reacao se conseguir"). Falls back to a full-length audio note if the cover art
is missing or the video build fails. Schedules the +24-48h post-sale follow-up.
Lands at DONE.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.graph.nodes import (
    conversation_id,
    emit_audio,
    emit_text,
    emit_video,
    get_brief,
    patch_extra,
    recipient_pronoun,
)
from app.graph.state import Stage
from app.media import storage
from app.music import kie, lyrics
from app.music import preview as preview_mod
from app.db import repo

logger = logging.getLogger(__name__)


async def deliver(state: dict) -> dict:
    brief = get_brief(state)
    name = brief.recipient_name or "essa pessoa"
    rec = recipient_pronoun(brief, unknown=name)
    variants = state.get("variants") or []
    chosen_id = state.get("chosen_variant")
    chosen = next(
        (v for v in variants if v.get("id") == chosen_id),
        variants[0] if variants else None,
    )

    conv_id = conversation_id(state)
    anon = conv_id or state.get("wa_jid", "anon")
    full_url = state.get("full_url")          # full-length 9:16 video (preferred)
    full_audio_url = state.get("full_audio_url")  # full-length audio (fallback)

    if chosen and not full_url and not full_audio_url:
        audio_url = chosen.get("audio_url") or chosen.get("audioUrl") or ""
        image_url = chosen.get("image_url") or chosen.get("imageUrl") or ""
        audio_data = await kie.download(audio_url)

        # Preferred deliverable: the whole song as a vertical 9:16 video.
        if image_url:
            try:
                image_data = await kie.download(image_url)
                clip = await asyncio.to_thread(
                    preview_mod.make_full_video, audio_data, image_data
                )
                path = storage.build_path(anon, prefix="full", ext="mp4")
                full_url = await storage.upload(path, clip, "video/mp4")
            except Exception:  # noqa: BLE001
                logger.exception("full video build failed; falling back to audio")
                full_url = None

        # Fallback: full-length audio (cover art missing / ffmpeg failed).
        if not full_url:
            path = storage.build_path(anon, prefix="full", ext="mp3")
            full_audio_url = await storage.upload(path, audio_data, "audio/mpeg")

    prompt = (state.get("extra") or {}).get("lyrics_prompt") or ""
    full = lyrics.full_lyrics(prompt)

    msgs = emit_text(state, ["Recebido 💛 to preparando aqui"])
    if full_url:
        msgs += emit_video(state, full_url, caption="A musica completa 🎶")
    elif full_audio_url:
        msgs += emit_audio(state, full_audio_url, caption="A musica completa 🎶")
    msgs += emit_text(state, [f"Aqui ela, completinha, pra ti e pro {name} pra sempre 🎶"])
    # Lyrics are NOT dumped by default — only on explicit request (router handles
    # "manda a letra"). Here we just let them know they can ask.
    if full:
        msgs += emit_text(state, ["Se quiser a letra escrita pra acompanhar, e so me pedir 💛"])
    msgs += emit_text(
        state,
        [
            f"Manda pra {rec} de um jeito especial 💛 grava a reacao se conseguir, e o "
            "melhor presente que tu vai ganhar de volta 🥹"
        ],
    )

    delivered_url = full_url or full_audio_url
    if conv_id:
        try:
            run_at = datetime.now(timezone.utc) + timedelta(hours=24)
            await repo.schedule_followup(conv_id, "postsale", run_at)
            if state.get("kie_task_id"):
                await repo.update_generation(
                    state["kie_task_id"], full_url=delivered_url, completed=True
                )
        except Exception:  # noqa: BLE001
            logger.exception("post-delivery bookkeeping failed")

    return {
        "paid": True,
        "full_url": full_url,
        "full_audio_url": full_audio_url,
        "lyrics_full": full,
        "stage": Stage.DONE.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
