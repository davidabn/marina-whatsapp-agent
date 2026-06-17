"""Parse raw Evolution API v2 ``messages.upsert`` webhook events.

Evolution's webhook payload is messy and version-dependent. This module's only
job is to turn one raw event into the stable :class:`InboundMessage` DTO that the
rest of the app consumes. Anything we cannot understand becomes ``None`` (the
caller simply ignores it) or ``kind="other"``.

Reference shape (Evolution API v2, ``messages.upsert``)::

    {
      "event": "messages.upsert",
      "instance": "marina",
      "data": {
        "key": {"remoteJid": "...@s.whatsapp.net", "fromMe": false, "id": "ABC"},
        "message": { ... union ... },
        "pushName": "Rafael",
        "messageTimestamp": 1718200000
      }
    }

``data`` is normally a dict, but some gateway/version combinations deliver it as
a single-element list — we tolerate both.
"""
from __future__ import annotations

from typing import Any, Optional

from app.evolution.types import InboundMessage


def _as_int(value: Any) -> Optional[int]:
    """Best-effort int coercion (timestamps arrive as int or numeric str)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unwrap_data(event: dict) -> Optional[dict]:
    """Return the ``data`` object as a dict, or ``None`` if unusable."""
    data = event.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


def parse_upsert(event: dict) -> InboundMessage | None:
    """Convert a raw ``messages.upsert`` event into an :class:`InboundMessage`.

    Returns ``None`` when the event is not a parseable single message (missing
    ``data``/``key``/``remoteJid``, or no ``message`` payload at all, e.g.
    protocol/receipt updates).
    """
    if not isinstance(event, dict):
        return None

    data = _unwrap_data(event)
    if data is None:
        return None

    key = data.get("key")
    if not isinstance(key, dict):
        return None

    jid = key.get("remoteJid")
    if not jid or not isinstance(jid, str):
        return None

    message = data.get("message")
    if not isinstance(message, dict) or not message:
        # No content (receipts, protocol updates, reactions-only, etc.).
        return None

    kind, text, mimetype = _classify(message)

    return InboundMessage(
        jid=jid,
        message_id=str(key.get("id") or ""),
        kind=kind,
        text=text,
        mimetype=mimetype,
        push_name=data.get("pushName"),
        from_me=bool(key.get("fromMe", False)),
        is_group=jid.endswith("@g.us"),
        timestamp=_as_int(data.get("messageTimestamp")),
        # Keep both message + key so the client can refetch media later.
        raw={"key": key, "message": message},
    )


def _classify(message: dict) -> tuple[str, Optional[str], Optional[str]]:
    """Map the Evolution ``message`` union to ``(kind, text, mimetype)``."""
    # Plain text bubble.
    conversation = message.get("conversation")
    if isinstance(conversation, str):
        return "text", conversation, None

    # Text with context (replies, link previews, etc.).
    ext = message.get("extendedTextMessage")
    if isinstance(ext, dict):
        return "text", ext.get("text"), None

    # Voice note / audio. Bytes are fetched later via client.fetch_media().
    audio = message.get("audioMessage")
    if isinstance(audio, dict):
        return "audio", None, audio.get("mimetype")

    # Image with optional caption.
    image = message.get("imageMessage")
    if isinstance(image, dict):
        return "image", image.get("caption"), image.get("mimetype")

    # Video with optional caption.
    video = message.get("videoMessage")
    if isinstance(video, dict):
        return "video", video.get("caption"), video.get("mimetype")

    # Document / file.
    document = message.get("documentMessage")
    if isinstance(document, dict):
        return "document", document.get("caption") or document.get("fileName"), document.get("mimetype")

    # Anything else (sticker, location, contact, reaction, ...).
    return "other", None, None
