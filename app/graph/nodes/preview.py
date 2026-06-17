"""Stage 7 — Entrega da previa (preview).

Resumed by the KIE webhook (runner.on_generation_complete injects `variants`).
Build a single ~45s preview from the first variant, upload it, then send the
partial lyrics teaser + the preview audio + an emotional "qual mexeu mais"
question. Sets stage=CHOICE and pre-selects the (single) variant.

Locked product decision: ONE preview, not two.
"""
from __future__ import annotations

import asyncio

from app.graph.nodes import conversation_id, emit_audio, emit_text, patch_extra
from app.graph.state import Stage
from app.media import storage
from app.music import kie, lyrics
from app.music import preview as preview_mod
from app.db import repo


async def preview(state: dict) -> dict:
    variants = state.get("variants") or []
    if not variants:
        # Nothing to preview (failed/empty generation) — leave the wait state.
        return {"outbound": state.get("outbound") or [], "extra": patch_extra(state, _next="end")}

    extra = state.get("extra") or {}
    prompt = extra.get("lyrics_prompt") or ""
    first = variants[0]
    audio_url = first.get("audio_url") or first.get("audioUrl") or ""

    data = await kie.download(audio_url)
    clip = await asyncio.to_thread(preview_mod.make_preview, data, start=0.0, duration=45.0)

    conv_id = conversation_id(state)
    path = storage.build_path(conv_id or state.get("wa_jid", "anon"), prefix="previews", ext="mp3")
    preview_url = await storage.upload(path, clip, "audio/mpeg")

    partial = lyrics.partial_lyrics(prompt)
    msgs = emit_text(state, ["Olha que linda a letra que saiu 🥹"])
    if partial:
        msgs += emit_text(state, [partial])
    msgs += emit_audio(state, preview_url, caption="Escuta com calma 💛")
    msgs += emit_text(state, ["Agora escuta com calma 💛", "Qual parte mexeu mais contigo?"])

    task_id = state.get("kie_task_id")
    if conv_id and task_id:
        await repo.update_generation(
            task_id, status="SUCCESS", variants=variants, preview_url=preview_url
        )

    return {
        "preview_url": preview_url,
        "variants": variants,
        "chosen_variant": first.get("id"),
        "stage": Stage.CHOICE.value,
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end", lyrics_prompt=prompt),
    }
