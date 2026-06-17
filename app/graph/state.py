"""Conversation state + stage machine contract for the LangGraph agent.

`Stage` is the single source of truth for where a conversation is. Every inbound
webhook restores state from the checkpointer, the router reads `stage`, and the
matching node runs. Transitions are pure-Python (see graph/router.py) — the LLM
never decides the stage.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Optional

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class Stage(str, Enum):
    WELCOME = "welcome"
    DISCOVERY_RECIPIENT = "discovery_recipient"   # name + relationship + singer gender
    DISCOVERY_STORY = "discovery_story"           # why special + phrase/nickname/date
    STYLE = "style"                               # musical style
    ANCHOR = "anchor"                             # how-it-works + value + consent (price 1st appears)
    SONGWRITER = "songwriter"                     # internal: brief -> KIE payload
    GENERATE = "generate"                         # submit to KIE
    GENERATION_WAIT = "generation_wait"           # awaiting KIE callback/poll
    PREVIEW = "preview"                           # send 45s preview + partial lyrics
    CHOICE = "choice"                             # customer reacts -> send PIX
    PIX = "pix"                                    # create MP charge, send copia-e-cola
    PIX_WAIT = "pix_wait"                          # awaiting payment webhook
    VERIFY = "verify"                              # payment confirmed
    DELIVER = "deliver"                            # send full song + full lyrics + UGC seed
    FOLLOWUP = "followup"                          # post-sale / cold follow-ups
    DONE = "done"
    NEEDS_HUMAN = "needs_human"                    # escalated to a human operator


class Relationship(str, Enum):
    ESPOSO = "esposo"
    ESPOSA = "esposa"
    NAMORADO = "namorado"
    NAMORADA = "namorada"
    MAE = "mae"
    PAI = "pai"
    FILHO = "filho"
    FILHA = "filha"
    AMIGO = "amigo"
    AMIGA = "amiga"
    OUTRO = "outro"


class Brief(BaseModel):
    """Everything we collect to write the song. Filled incrementally by nodes."""
    recipient_name: Optional[str] = None
    relationship: Optional[Relationship] = None
    # Gender of the VOICE that sings = the buyer/singer, NOT the recipient.
    singer_gender: Optional[str] = Field(None, description="'m' or 'f'")
    story: Optional[str] = None
    special_phrases: list[str] = Field(default_factory=list)
    nickname: Optional[str] = None
    special_date: Optional[str] = None
    style_request: Optional[str] = None          # free text, e.g. "tipo Henrique e Juliano"

    def has_recipient(self) -> bool:
        return bool(self.recipient_name and self.relationship and self.singer_gender)

    def has_story(self) -> bool:
        return bool(self.story)

    def has_style(self) -> bool:
        return bool(self.style_request)


class ConversationState(TypedDict, total=False):
    """LangGraph state. Persisted by PostgresSaver, thread_id = wa_jid."""
    wa_jid: str                      # WhatsApp JID (thread id)
    messages: Annotated[list, add_messages]   # LLM chat history
    stage: str                       # Stage value
    brief: dict                      # Brief.model_dump()
    generation_consent: bool
    kie_task_id: Optional[str]
    variants: list[dict]             # [{id, audio_url, title}]
    preview_url: Optional[str]
    full_url: Optional[str]
    lyrics_full: Optional[str]
    chosen_variant: Optional[str]
    order_id: Optional[str]
    mp_payment_id: Optional[str]
    paid: bool
    regen_count: int
    needs_human: bool
    # Transient per-turn fields (not persisted meaningfully):
    inbound_text: str                # normalized text of the current inbound turn
    outbound: list[str]              # short message bubbles the node wants to send
    extra: dict[str, Any]
