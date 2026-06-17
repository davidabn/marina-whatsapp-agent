"""OpenAI speech-to-text for inbound WhatsApp voice notes (pt-BR).

Thin wrapper over AsyncOpenAI audio transcription. Failures are swallowed and
return "" so the conversation flow never crashes on a bad audio blob.
"""
from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI

from app.config import settings

log = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def transcribe(
    audio: bytes,
    *,
    mimetype: str = "audio/ogg",
    filename: str = "audio.ogg",
) -> str:
    """Transcribe audio bytes to pt-BR text; return "" on empty input or failure."""
    if not audio:
        return ""
    try:
        resp = await _get_client().audio.transcriptions.create(
            model=settings.openai_transcribe_model,
            file=(filename, audio, mimetype),
            language="pt",
        )
        return (getattr(resp, "text", "") or "").strip()
    except Exception:
        log.exception("STT transcription failed")
        return ""
