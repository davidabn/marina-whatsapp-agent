"""Stages 2 & 3 — Descoberta (discovery_recipient + discovery_story).

discovery_recipient: pull recipient_name + relationship (+ singer_gender when a
cue lets us), mirror the name, and when we know who it is, bridge into the
emotional "why is this person special" question (which the story node answers).

discovery_story: collect the story, mirror ONE specific detail, ask for a
phrase / nickname / date, then close discovery and hand off to STYLE. This node
also serves the OBJECTION_LYRICS regen path: the router parks the conversation
here (regen flag) so a fresh detail can be gathered before regenerating.

Completeness here uses name+relationship (NOT Brief.has_recipient, which also
requires singer_gender — sometimes unknowable for mae/pai/filho — and would
deadlock discovery; the songwriter safely defaults vocalGender when missing).
"""
from __future__ import annotations

from app.graph.nodes import (
    DELETE,
    apply_updates,
    dump_brief,
    emit_text,
    get_brief,
    history,
    patch_extra,
)
from app.graph.state import Brief, Stage
from app.llm import extract, reply


def _recipient_complete(brief: Brief) -> bool:
    return bool(brief.recipient_name and brief.relationship)


def _has_extras(brief: Brief) -> bool:
    return bool(brief.special_phrases or brief.nickname or brief.special_date)


def _rel(brief: Brief) -> str:
    return getattr(brief.relationship, "value", brief.relationship) or ""


def _ask(state: dict, brief: Brief, stage: str, msgs: list) -> dict:
    """Standard "stay and re-ask" return."""
    return {
        "stage": stage,
        "brief": dump_brief(brief),
        "outbound": state["outbound"],
        "messages": msgs,
        "extra": patch_extra(state, _next="end"),
    }


# --------------------------------------------------------------------------- #
# discovery_recipient
# --------------------------------------------------------------------------- #
async def discovery_recipient(state: dict) -> dict:
    brief = get_brief(state)
    updates = await extract.extract_slots(
        history(state), state.get("inbound_text", ""), brief, Stage.DISCOVERY_RECIPIENT.value
    )
    brief = apply_updates(brief, updates)

    if _recipient_complete(brief):
        instruction = (
            f"Voce ja sabe que a musica e pra {brief.recipient_name} (relacao: "
            f"{_rel(brief)}). Reaja com carinho espelhando o NOME ('{brief.recipient_name} "
            "que linda', 'aaah que amor'). Depois, em outra bolha, faca a pergunta "
            "emocional aberta: o que fez essa pessoa se apaixonar / o que "
            f"{brief.recipient_name} tem de especial que ninguem mais tem. Curto."
        )
        bubbles = await reply.compose(history(state), instruction, brief=brief)
        msgs = emit_text(state, bubbles)
        return _ask(state, brief, Stage.DISCOVERY_STORY.value, msgs)

    if brief.recipient_name and not brief.relationship:
        instruction = (
            f"Espelhe o nome '{brief.recipient_name}' com carinho e pergunte o que "
            "essa pessoa e do cliente (esposo, namorado, mae, pai, amiga...). Curto."
        )
    else:
        instruction = (
            "Ainda nao sabemos pra quem e a musica. Pergunte com curiosidade e "
            "carinho pra quem ela e (o nome e a relacao). Curto."
        )
    bubbles = await reply.compose(history(state), instruction, brief=brief)
    msgs = emit_text(state, bubbles)
    return _ask(state, brief, Stage.DISCOVERY_RECIPIENT.value, msgs)


# --------------------------------------------------------------------------- #
# discovery_story
# --------------------------------------------------------------------------- #
async def discovery_story(state: dict) -> dict:
    extra = state.get("extra") or {}
    regen = bool(extra.get("regen")) and extra.get("regen_kind") == "lyrics"

    brief = get_brief(state)
    had_story = brief.has_story()
    updates = await extract.extract_slots(
        history(state), state.get("inbound_text", ""), brief, Stage.DISCOVERY_STORY.value
    )
    got_new = bool(updates)
    brief = apply_updates(brief, updates)

    # --- OBJECTION_LYRICS regen path ------------------------------------- #
    if regen:
        if got_new:
            return _to_regen(state, brief)
        instruction = (
            "A pessoa nao gostou da letra (achou generica). Sem defender, peca com "
            "carinho UM detalhe a mais especifico sobre eles — um momento, uma "
            "frase, algo que viveram — que voce refaz puxando esse lado. Curto."
        )
        bubbles = await reply.compose(history(state), instruction, brief=brief)
        msgs = emit_text(state, bubbles)
        return _ask(state, brief, Stage.DISCOVERY_STORY.value, msgs)

    # --- turn A: receiving (or still missing) the story ------------------ #
    if not had_story:
        if not brief.has_story():
            instruction = (
                "Ainda nao temos a historia. Peca de novo, com carinho e interesse "
                "real, o que torna essa pessoa especial / o que ninguem mais tem. Curto."
            )
            bubbles = await reply.compose(history(state), instruction, brief=brief)
            msgs = emit_text(state, bubbles)
            return _ask(state, brief, Stage.DISCOVERY_STORY.value, msgs)
        if _has_extras(brief):
            return await _to_style(state, brief)
        instruction = (
            "A pessoa acabou de contar a historia. Espelhe UM detalhe especifico "
            "do que ela disse (mostre que voce leu de verdade, nada generico). "
            "Depois, em outra bolha, pergunte se tem algum momento, frase, apelido "
            "ou data que eles tem e que ela queira que apareca na musica. Curto."
        )
        bubbles = await reply.compose(history(state), instruction, brief=brief)
        msgs = emit_text(state, bubbles)
        return _ask(state, brief, Stage.DISCOVERY_STORY.value, msgs)

    # --- turn B: the extras answer (or "pode usar o que falei") ---------- #
    return await _to_style(state, brief)


async def _to_style(state: dict, brief: Brief) -> dict:
    instruction = (
        "Confirme com carinho que voce ja tem tudo ('Perfeito, ja to com tudo "
        "aqui'). Depois pergunte qual estilo musical combina mais com eles, "
        "oferecendo um menu CURTO (sertanejo, MPB romantica, pop acustica, "
        "pagode, gospel, forro) ou um artista de referencia que voce adapta. Curto."
    )
    bubbles = await reply.compose(history(state), instruction, brief=brief)
    msgs = emit_text(state, bubbles)
    return _ask(state, brief, Stage.STYLE.value, msgs)


def _to_regen(state: dict, brief: Brief) -> dict:
    """New detail captured for a lyrics objection -> rebuild the song."""
    return {
        "stage": Stage.SONGWRITER.value,
        "brief": dump_brief(brief),
        "outbound": state["outbound"],
        "extra": patch_extra(
            state, _next="songwriter", _is_regen=True, regen=DELETE, regen_kind=DELETE
        ),
    }
