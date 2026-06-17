"""Normalized inbound message DTO.

The Evolution webhook payload is messy and version-dependent. `parser.py`
converts a raw `messages.upsert` event into this stable shape, which the rest of
the app (router, graph, webhooks) consumes. Foundation type — shared contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# kind ∈ {"text", "audio", "image", "video", "document", "other"}


@dataclass
class InboundMessage:
    jid: str                         # remoteJid, e.g. "5547999999999@s.whatsapp.net"
    message_id: str                  # key.id (dedupe)
    kind: str = "text"
    text: Optional[str] = None       # text body, or caption for media
    media_id: Optional[str] = None   # opaque id/ref used by client.fetch_media()
    mimetype: Optional[str] = None
    push_name: Optional[str] = None
    from_me: bool = False
    is_group: bool = False
    timestamp: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def phone(self) -> str:
        return self.jid.split("@", 1)[0]

    @property
    def is_actionable(self) -> bool:
        """Skip our own messages and group chats."""
        return not self.from_me and not self.is_group
