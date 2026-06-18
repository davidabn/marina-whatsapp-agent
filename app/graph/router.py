"""Entry router: stage + intent dispatch with global objection handling.

Every invocation starts here. The router:

1. Handles external resume EVENTS first (KIE webhook / payment webhook /
   scheduler) — these bypass intent classification.
2. On a real user turn, classifies intent ONCE and catches GLOBAL intents
   before per-stage dispatch:
   - IS_BOT / WANTS_HUMAN  -> set needs_human, gentle handoff line, END.
   - OBJECTION_STYLE/LYRICS (only once past the preview) -> route to the style /
     story regen path, respecting regen_count vs settings.max_free_regenerations.
   - TOO_EXPENSIVE / WILL_THINK / PAY_LATER (around the money moment) -> emit the
     scripted recovery copy from whatsapp-script.md and STAY (do not advance).
3. Otherwise dispatches to the node for the current stage. While we wait on KIE
   or a pix, a user message just gets a warm holding line.

The router itself is a NODE: it can mutate state (emit copy, set needs_human)
and stores the chosen successor in `extra['_next']`, which a conditional edge
reads to pick the target node (or END).
"""
from __future__ import annotations

import re

from app.config import settings
from app.graph.nodes import DELETE, emit_text, patch_extra
from app.graph.state import Stage
from app.llm import extract
from app.llm.extract import Intent
from app.music import lyrics

# Customer explicitly asking for the WRITTEN lyrics (a request, not an objection
# like "nao gostei da letra" — those carry no request verb and stay with the
# intent classifier). Only answered once the song's lyrics actually exist.
_WANTS_LYRICS = re.compile(
    r"\b(?:manda|mandar|me\s+manda|envia|enviar|me\s+envia|passa|passar|"
    r"quero|queria|ver|cad[eê]|tem)\b[^?.!\n]{0,25}\bletra\b"
    r"|\bletra\b[^?.!\n]{0,25}(?:escrit|por\s+escrito|pra\s+ler|pra\s+acompanhar)"
    r"|^\s*(?:a\s+)?letra\s+(?:por\s+favor|pf|completa|inteira|escrita)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# Scripted recovery copy (verbatim from sales/whatsapp-script.md, section 4)
# --------------------------------------------------------------------------- #
_TOO_EXPENSIVE = [
    "Eu te entendo 💛 so queria te lembrar que e menos que um buque de flores que "
    "dura 3 dias — essa musica ele vai poder escutar pra sempre, sempre que sentir "
    "saudade tua. E e a historia de VOCES, ninguem mais no mundo tem",
]
_WILL_THINK = [
    "Imagina 💛 fica a vontade",
    "So uma coisa: a musica ta aqui prontinha, esperando. Se tu quiser fazer ainda "
    "hoje pra surpreender ele antes de dormir, e so me chamar 💛",
]
_PAY_LATER = [
    "Olha, pra eu te mandar a completa hoje precisa ser agora — assim eu nao perco a "
    "musica que gerei especialmente pra voces. Consegue fazer o pix em uns minutinhos?",
]
_HANDOFF = [
    "Claro 💛 vou pedir pra uma pessoa da equipe falar contigo direitinho, ta? "
    "So um minutinho",
]
_REGEN_CAP = [
    "Ja refiz com todo carinho 💛 deixa eu olhar isso com mais atencao pra fazer do "
    "jeito certo pra voces",
]
_HOLD_GENERATION = ["Ja ta quase ✨ to finalizando com carinho pra ele 💛"]
_HOLD_PIX = ["Assim que cair o pix aqui eu ja te mando ela completinha 💛"]

# Stages where money/objection recovery copy makes sense.
_PRICE_STAGES = {
    Stage.ANCHOR.value,
    Stage.CHOICE.value,
    Stage.PIX.value,
    Stage.PIX_WAIT.value,
}
# Stages where a style/lyrics objection means "regenerate".
_REGEN_STAGES = {
    Stage.GENERATION_WAIT.value,
    Stage.PREVIEW.value,
    Stage.CHOICE.value,
    Stage.PIX.value,
    Stage.PIX_WAIT.value,
}

# stage -> node for the straightforward NORMAL dispatch.
_STAGE_NODE = {
    Stage.WELCOME.value: "welcome",
    Stage.DISCOVERY_RECIPIENT.value: "discovery_recipient",
    Stage.DISCOVERY_STORY.value: "discovery_story",
    Stage.STYLE.value: "style",
    Stage.ANCHOR.value: "anchor",
    Stage.SONGWRITER.value: "songwriter",
    Stage.GENERATE.value: "generate",
    Stage.PREVIEW.value: "preview",
    Stage.CHOICE.value: "choice",
    Stage.PIX.value: "pix",
    Stage.VERIFY.value: "deliver",
    Stage.DELIVER.value: "deliver",
    Stage.FOLLOWUP.value: "followup",
    Stage.DONE.value: "followup",
}


def _route(state: dict, target: str, **extra_changes) -> dict:
    # event is one-shot: always cleared as we leave the router.
    return {"extra": patch_extra(state, _next=target, event=DELETE, **extra_changes)}


def _emit_stay(state: dict, bubbles: list[str], **state_updates) -> dict:
    msgs = emit_text(state, bubbles)
    out = {
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end", event=DELETE),
    }
    out.update(state_updates)
    return out


def _regen_allowed(state: dict) -> bool:
    return int(state.get("regen_count") or 0) < int(settings.max_free_regenerations)


def _past_preview(state: dict, stage: str) -> bool:
    return bool(state.get("variants")) or stage in _REGEN_STAGES


async def router(state: dict) -> dict:
    extra = state.get("extra") or {}
    event = extra.get("event")
    stage = state.get("stage") or Stage.WELCOME.value

    # 1) External resume events bypass intent classification.
    if event == "generation_done":
        return _route(state, "preview")
    if event == "video_done":
        return _route(state, "video_ready")
    if event == "video_timeout":
        return _route(state, "preview", force_audio=True)
    if event == "payment_done":
        return _route(state, "deliver")
    if event == "followup":
        return _route(state, "followup")

    inbound = (state.get("inbound_text") or "").strip()
    if not inbound:
        return _route(state, "end")

    # Customer explicitly asks for the written lyrics -> send them and stay put.
    # We don't deliver lyrics by default, only on request (and only once they exist).
    if _WANTS_LYRICS.search(inbound):
        prompt = (state.get("extra") or {}).get("lyrics_prompt") or ""
        full = lyrics.full_lyrics(prompt) if prompt else ""
        if full:
            return _emit_stay(state, ["Claro 💛 aqui a letra:", full])

    intent = await extract.classify_intent(inbound, stage)

    # 2a) Hard global intents.
    if intent in (Intent.IS_BOT, Intent.WANTS_HUMAN):
        return _emit_stay(
            state, _HANDOFF, needs_human=True, stage=Stage.NEEDS_HUMAN.value
        )

    # 2b) Style / lyrics objections -> regen (only once we have a song).
    if intent == Intent.OBJECTION_STYLE and _past_preview(state, stage):
        if _regen_allowed(state):
            return _route(state, "style", regen=True, regen_kind="style")
        return _emit_stay(state, _REGEN_CAP, needs_human=True)
    if intent == Intent.OBJECTION_LYRICS and _past_preview(state, stage):
        if _regen_allowed(state):
            return _route(state, "discovery_story", regen=True, regen_kind="lyrics")
        return _emit_stay(state, _REGEN_CAP, needs_human=True)

    # 2c) Money objections around the decision point -> scripted copy, stay.
    if stage in _PRICE_STAGES:
        if intent == Intent.TOO_EXPENSIVE:
            return _emit_stay(state, _TOO_EXPENSIVE)
        if intent == Intent.WILL_THINK:
            return _emit_stay(state, _WILL_THINK)
        if intent == Intent.PAY_LATER:
            return _emit_stay(state, _PAY_LATER)

    # 3) Holding lines while we wait on an external event.
    if stage == Stage.GENERATION_WAIT.value:
        return _emit_stay(state, _HOLD_GENERATION)
    if stage == Stage.PIX_WAIT.value:
        return _emit_stay(state, _HOLD_PIX)
    if stage == Stage.NEEDS_HUMAN.value:
        return _route(state, "end")

    # 4) Normal per-stage dispatch.
    return _route(state, _STAGE_NODE.get(stage, "welcome"))
