"""Compose Marina's outbound message bubbles (with a tone post-filter).

Every conversational node funnels through `compose`: it pairs the persona system
prompt with a node-specific `instruction` ("what to accomplish THIS turn"), runs
the model, splits the answer into short WhatsApp bubbles, and enforces tone. If a
bubble trips `tone_violations`, it retries once with a correction; if it still
slips, the offending banned emoji is stripped so we never ship a salesy bubble.
"""
from __future__ import annotations

import logging
import re

from app.graph.state import Brief
from app.llm.llm import get_chat
from app.llm.persona import BANNED_EMOJIS, BANNED_PHRASES, PERSONA_SYSTEM, tone_violations

logger = logging.getLogger(__name__)

# Explicit bubble separator the model may emit; we also split on blank lines.
_SENTINEL = "|||"
_BLANK_LINE = re.compile(r"\n\s*\n")
_BUBBLE_FORMAT = (
    "Responda como a Marina em mensagens CURTAS, uma por linha, no ritmo de uma "
    "conversa real de WhatsApp. Separe cada bolha com uma linha em branco (ou com "
    f"'{_SENTINEL}'). Nao escreva paragrafos longos."
)


def split_bubbles(text: str) -> list[str]:
    """Split raw model output into clean WhatsApp bubbles.

    Robust to the model returning a single block: splits first on the explicit
    sentinel, then on blank lines, finally falls back to per-line splitting.
    Strips bullet/quote markers and drops empties.
    """
    if not text:
        return []
    text = text.strip()

    if _SENTINEL in text:
        parts = text.split(_SENTINEL)
    else:
        parts = _BLANK_LINE.split(text)
        # Single block with several short lines -> treat each line as a bubble.
        if len(parts) == 1:
            lines = [ln for ln in text.split("\n") if ln.strip()]
            if len(lines) > 1:
                parts = lines

    bubbles: list[str] = []
    for part in parts:
        cleaned = part.strip().lstrip("-*•>").strip()
        if cleaned:
            bubbles.append(cleaned)
    return bubbles or ([text] if text else [])


def _strip_banned(text: str) -> str:
    """Last-resort sanitizer: remove banned emojis AND banned phrases/words
    (e.g. 'caraca', commercial jargon) so they never ship even if the retry
    failed to drop them."""
    for emoji in BANNED_EMOJIS:
        text = text.replace(emoji, "")
    for phrase in BANNED_PHRASES:
        text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
    # tidy leftovers: collapse spaces and remove a space left before punctuation
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.!?…])", r"\1", text)
    return text.strip(" ,").strip()


def gender_hint(brief: Brief | None) -> str:
    """One-line gender/agreement guidance for the LLM, derived from the brief.

    Keeps Marina from defaulting to feminine forms for a male buyer and uses the
    right pronoun for the recipient.
    """
    if brief is None:
        return ""
    bits: list[str] = []
    bg = brief.buyer_gender()
    if bg == "m":
        bits.append(
            "O CLIENTE com quem voce fala e HOMEM: use concordancia MASCULINA ao se "
            "dirigir a ele (ex: 'tranquilo', 'preparado'). Nunca use formas femininas pra ele."
        )
    elif bg == "f":
        bits.append(
            "A CLIENTE com quem voce fala e MULHER: use concordancia FEMININA ao se "
            "dirigir a ela (ex: 'tranquila', 'preparada')."
        )
    else:
        bits.append(
            "Voce ainda NAO sabe o genero do cliente: use linguagem NEUTRA, nao assuma "
            "homem nem mulher (evite 'tranquilo/tranquila'; prefira 'pode deixar comigo')."
        )
    rg = brief.recipient_gender()
    name = brief.recipient_name or "o presenteado"
    if rg == "m":
        bits.append(f"O presenteado ({name}) e HOMEM: use ele/dele.")
    elif rg == "f":
        bits.append(f"A presenteada ({name}) e MULHER: use ela/dela.")
    return "GENERO E CONCORDANCIA: " + " ".join(bits)


async def compose(
    history: list,
    instruction: str,
    *,
    brief: Brief | None = None,
    temperature: float = 0.7,
) -> list[str]:
    """Generate clean, tone-compliant message bubbles for the current turn."""
    brief_ctx = ""
    if brief is not None:
        known = brief.model_dump(exclude_none=True)
        if known:
            brief_ctx = f"\n\nO QUE JA SABEMOS (brief): {known}"

    gh = gender_hint(brief)
    gh_block = f"\n\n{gh}" if gh else ""

    llm = get_chat(temperature=temperature)

    def _messages(extra_instruction: str = "") -> list:
        system_task = (
            f"OBJETIVO DESTE TURNO:\n{instruction}{brief_ctx}{gh_block}\n\n{_BUBBLE_FORMAT}"
            f"{extra_instruction}"
        )
        return [
            ("system", PERSONA_SYSTEM),
            ("system", system_task),
            *list(history or []),
        ]

    async def _run(extra_instruction: str = "") -> list[str]:
        resp = await llm.ainvoke(_messages(extra_instruction))
        content = getattr(resp, "content", resp)
        return split_bubbles(content if isinstance(content, str) else str(content))

    bubbles = await _run()

    if any(tone_violations(b) for b in bubbles):
        correction = (
            "\n\nCORRECAO IMPORTANTE: a resposta anterior usou um emoji, jargao ou "
            "expressao proibida. Reescreva sem nenhum emoji da lista proibida "
            "(🚀 ✅ 💰 🔥 ⚡), sem jargao comercial ('nosso servico', 'posso prosseguir', "
            "'fechamos', 'promocao', etc.) e sem girias/interjeicoes de susto ou "
            "palavroes (ex.: 'caraca'). Use so o tom caloroso e intimo da Marina."
        )
        try:
            bubbles = await _run(correction)
        except Exception:  # pragma: no cover - network guard
            logger.exception("compose retry failed; sanitizing first attempt")

        # Last resort: strip any offending banned emoji/phrase from each bubble.
        if any(tone_violations(b) for b in bubbles):
            bubbles = [_strip_banned(b) for b in bubbles]

    return [b for b in bubbles if b]
