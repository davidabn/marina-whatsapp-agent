"""Async Evolution API (WhatsApp) client for the Marina sales agent.

Targets **Evolution API v2**. All requests carry the ``apikey`` header and JSON
bodies. One shared :class:`httpx.AsyncClient` is reused for connection pooling;
call :meth:`EvolutionClient.aclose` on shutdown.

Endpoint conventions (v2)::

    POST {base}/message/sendText/{instance}
    POST {base}/message/sendWhatsAppAudio/{instance}
    POST {base}/message/sendMedia/{instance}
    POST {base}/chat/sendPresence/{instance}
    POST {base}/chat/getBase64FromMediaMessage/{instance}

Outbound bodies use ``number`` = the recipient phone (digits, no ``@s.whatsapp``
suffix needed — Evolution resolves it).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
from typing import Any, Optional

import httpx

from app.config import settings
from app.evolution.types import InboundMessage

logger = logging.getLogger(__name__)


class EvolutionClient:
    """Thin async wrapper over the Evolution v2 REST API."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        instance: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or settings.evolution_base_url).rstrip("/")
        self.api_key = api_key or settings.evolution_api_key
        self.instance = instance or settings.evolution_instance
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "apikey": self.api_key,
                "Content-Type": "application/json",
            },
        )

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "EvolutionClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- internal ----------------------------------------------------------
    async def _post(self, path: str, body: dict) -> dict:
        """POST JSON and return the decoded body, raising on HTTP errors."""
        resp = await self._client.post(path, json=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {}

    # -- text --------------------------------------------------------------
    async def send_text(
        self, number: str, text: str, *, delay_ms: Optional[int] = None
    ) -> dict:
        """Send a single text bubble.

        ``delay`` (ms) makes Evolution show a brief "typing" before delivering.
        """
        body: dict[str, Any] = {"number": number, "text": text}
        if delay_ms is not None:
            body["delay"] = delay_ms
        return await self._post(f"/message/sendText/{self.instance}", body)

    async def send_text_sequence(self, number: str, messages: list[str]) -> None:
        """Send several short bubbles the way a human texts.

        For each bubble: compute a randomized human-like pause, keep "composing"
        presence visible for that whole duration, then send the text.
        """
        for text in messages:
            if not text:
                continue
            pause = self._typing_delay(text)
            await self.send_presence(number, "composing", delay_ms=int(pause * 1000))
            await asyncio.sleep(pause)
            await self.send_text(number, text)

    @staticmethod
    def _typing_delay(text: str) -> float:
        """Randomized human-like pause (seconds): a "thinking" beat + a jittered
        typing time proportional to bubble length, clamped to a sane window.
        """
        base = len(text or "") * settings.typing_per_char
        think = random.uniform(settings.typing_think_min, settings.typing_think_max)
        jitter = random.uniform(1 - settings.typing_jitter, 1 + settings.typing_jitter)
        raw = think + base * jitter
        return max(settings.typing_min_seconds, min(settings.typing_max_seconds, raw))

    # -- presence ----------------------------------------------------------
    async def send_presence(
        self, number: str, presence: str = "composing", delay_ms: int = 1200
    ) -> None:
        """Set chat presence (e.g. "composing"/"recording"). Best-effort.

        Presence is cosmetic; never let a failure here break the conversation.
        """
        body = {"number": number, "presence": presence, "delay": delay_ms}
        try:
            await self._post(f"/chat/sendPresence/{self.instance}", body)
        except Exception as exc:  # noqa: BLE001 — presence is best-effort
            logger.debug("sendPresence failed (ignored): %s", exc)

    # -- audio -------------------------------------------------------------
    async def send_audio(self, number: str, audio_url: str) -> dict:
        """Send an inline WhatsApp voice note from a URL.

        Used both for the 45s preview and the full song. Shows a "recording…"
        presence and pauses a random beat first so the audio doesn't pop in
        instantly after the preceding text.
        """
        pause = random.uniform(settings.audio_pre_delay_min, settings.audio_pre_delay_max)
        await self.send_presence(number, "recording", delay_ms=int(pause * 1000))
        await asyncio.sleep(pause)
        body = {"number": number, "audio": audio_url}
        return await self._post(f"/message/sendWhatsAppAudio/{self.instance}", body)

    # -- media -------------------------------------------------------------
    async def send_media(
        self,
        number: str,
        media_url: str,
        *,
        mediatype: str = "document",
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        mimetype: Optional[str] = None,
    ) -> dict:
        """Send an image/video/document by URL.

        ``mediatype`` ∈ {"image", "video", "document", "audio"}.
        """
        body: dict[str, Any] = {
            "number": number,
            "mediatype": mediatype,
            "media": media_url,
        }
        if caption is not None:
            body["caption"] = caption
        if file_name is not None:
            body["fileName"] = file_name
        if mimetype is not None:
            body["mimetype"] = mimetype
        return await self._post(f"/message/sendMedia/{self.instance}", body)

    async def fetch_media(self, inbound: InboundMessage) -> bytes:
        """Download the decrypted bytes of an inbound media message.

        Evolution re-encrypts WhatsApp media; the only reliable way to get the
        bytes is to ask the gateway to base64-encode them for us. We pass back
        the raw message captured by the parser.

        NOTE (version variance): the v2 endpoint expects
        ``{"message": <raw message object>}`` and returns the payload under a
        ``base64`` field — but some builds nest it (``data.base64``,
        ``media.base64``) or name it ``buffer``. We probe the common spots.
        """
        body = {"message": inbound.raw, "convertToMp4": False}
        result = await self._post(
            f"/chat/getBase64FromMediaMessage/{self.instance}", body
        )
        b64 = _extract_base64(result)
        if not b64:
            raise ValueError(
                "Evolution getBase64FromMediaMessage returned no base64 payload "
                f"(keys: {sorted(result)[:10]})"
            )
        return base64.b64decode(b64)


def _extract_base64(result: dict) -> Optional[str]:
    """Find the base64 string across known Evolution response shapes."""
    if not isinstance(result, dict):
        return None
    for key in ("base64", "buffer", "media"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    nested = result.get("data")
    if isinstance(nested, dict):
        return _extract_base64(nested)
    return None
