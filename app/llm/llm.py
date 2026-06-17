"""Shared ChatOpenAI factory.

Every LLM-touching module (extract, reply, songwriter) builds its client through
`get_chat` so the API key / default model live in exactly one place. The actual
`langchain_openai` import is lazy so that importing this module (and therefore the
brain modules) does not require the package at import time — handy for unit tests
that monkeypatch `get_chat` and never hit the network.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langchain_openai import ChatOpenAI


def get_chat(model: str | None = None, temperature: float = 0.6) -> "ChatOpenAI":
    """Build a configured `langchain_openai.ChatOpenAI`.

    Args:
        model: model id; defaults to `settings.openai_model` (gpt-4o-mini).
        temperature: sampling temperature.
    """
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        api_key=settings.openai_api_key,
        model=model or settings.openai_model,
        temperature=temperature,
    )
