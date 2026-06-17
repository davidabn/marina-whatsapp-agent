"""Stage 9 — Entrega completa (deliver).

Resumed by the payment webhook (runner.on_payment_event, after the charge is
confirmed approved). Download the chosen variant in full, upload it, then send
the full song + the complete lyrics + the UGC seed ("grava a reacao se
conseguir"). Schedules the +24-48h post-sale follow-up. Lands at DONE.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.graph.nodes import (
    conversation_id,
    emit_audio,
    emit_text,
    get_brief,
    patch_extra,
)
from app.graph.state import Stage
from app.media import storage
from app.music import kie, lyrics
from app.db import repo

logger = logging.getLogger(__name__)


async def deliver(state: dict) -> dict:
    brief = get_brief(state)
    name = brief.recipient_name or "ele"
    variants = state.get("variants") or []
    chosen_id = state.get("chosen_variant")
    chosen = next(
        (v for v in variants if v.get("id") == chosen_id),
        variants[0] if variants else None,
    )

    conv_id = conversation_id(state)
    full_url = state.get("full_url")
    if chosen and not full_url:
        audio_url = chosen.get("audio_url") or chosen.get("audioUrl") or ""
        data = await kie.download(audio_url)
        path = storage.build_path(conv_id or state.get("wa_jid", "anon"), prefix="full", ext="mp3")
        full_url = await storage.upload(path, data, "audio/mpeg")

    prompt = (state.get("extra") or {}).get("lyrics_prompt") or ""
    full = lyrics.full_lyrics(prompt)

    msgs = emit_text(state, ["Recebido 💛 to preparando aqui"])
    if full_url:
        msgs += emit_audio(state, full_url, caption="A musica completa 🎶")
    msgs += emit_text(state, [f"Aqui ela, completinha, pra ti e pro {name} pra sempre 🎶"])
    if full:
        msgs += emit_text(state, [f"E a letra inteira:\n\n{full}"])
    msgs += emit_text(
        state,
        [
            "Manda pra ele de um jeito especial 💛 grava a reacao se conseguir, e o "
            "melhor presente que tu vai ganhar de volta 🥹"
        ],
    )

    if conv_id:
        try:
            run_at = datetime.now(timezone.utc) + timedelta(hours=24)
            await repo.schedule_followup(conv_id, "postsale", run_at)
            if state.get("kie_task_id"):
                await repo.update_generation(state["kie_task_id"], full_url=full_url, completed=True)
        except Exception:  # noqa: BLE001
            logger.exception("post-delivery bookkeeping failed")

    return {
        "paid": True,
        "full_url": full_url,
        "lyrics_full": full,
        "stage": Stage.DONE.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }
