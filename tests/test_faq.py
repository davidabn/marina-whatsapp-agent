"""Logic-only tests for the grounded FAQ facts + per-stage doubt instruction.

No network: pure string assertions over FAQ_FACTS / faq_instruction.
"""
from __future__ import annotations

from app.config import settings
from app.llm import faq


# --------------------------------------------------------------------------- #
# FAQ_FACTS — corrected ground truth (checkout link, not a raw pix key)
# --------------------------------------------------------------------------- #
def test_faq_facts_use_checkout_link_not_pix_key():
    low = faq.FAQ_FACTS.lower()
    assert "link" in low
    assert "checkout" in low
    assert "cartao" in low
    # The script is outdated on payment; the facts must NOT push a raw pix key.
    assert "chave pix" not in low
    assert "copia" not in low


def test_faq_facts_price_from_settings():
    assert settings.price_reais in faq.FAQ_FACTS      # "29,90"
    # no stray hardcoded other price
    assert "19,90" not in faq.FAQ_FACTS
    assert "39,90" not in faq.FAQ_FACTS


# --------------------------------------------------------------------------- #
# faq_instruction — per-stage nudge back into the funnel
# --------------------------------------------------------------------------- #
def test_faq_instruction_nudges_per_stage():
    assert "gerar" in faq.faq_instruction("anchor")
    pixwait = faq.faq_instruction("pix_wait")
    assert "link" in pixwait and "pagamento" in pixwait
    assert "especial" in faq.faq_instruction("discovery_story")


def test_faq_instruction_defers_price_in_discovery():
    for stage in ("welcome", "discovery_recipient", "discovery_story", "style"):
        instr = faq.faq_instruction(stage)
        assert "INSISTIR" in instr
        assert "acessivel" in instr
    # past discovery, no deferral rule
    for stage in ("anchor", "choice"):
        assert "INSISTIR" not in faq.faq_instruction(stage)


def test_faq_instruction_grounding_clause():
    instr = faq.faq_instruction("discovery_story")
    assert "NUNCA invente" in instr          # anti-hallucination guard
    assert faq.FAQ_FACTS in instr            # the facts block is embedded


def test_faq_instruction_bot_deflection():
    base = faq.faq_instruction("discovery_story")
    deflect = faq.faq_instruction("discovery_story", deflect_bot=True)
    assert deflect != base
    assert "SEM confirmar" in deflect
    # it instructs NOT to use these words (the directive itself names them)
    assert "SEM usar" in deflect
    assert "SEM confirmar" not in base
