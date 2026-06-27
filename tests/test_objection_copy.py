"""Copy guards: the buquê/flowers value-anchor only appears when breaking a money
objection, and only for a romantic relationship."""
from __future__ import annotations

from app.graph.nodes.anchor import _explanation_bubbles
from app.graph.router import _too_expensive
from app.graph.state import Brief, Relationship


def test_anchor_explanation_has_no_buque():
    brief = Brief(recipient_name="Marcio", relationship=Relationship.ESPOSO, singer_gender="f")
    joined = " ".join(_explanation_bubbles(brief)).lower()
    assert "buque" not in joined
    assert "flores" not in joined
    # still the deterministic 1/2/3 + consent framing
    assert any(b.startswith("3.") for b in _explanation_bubbles(brief))
    assert _explanation_bubbles(brief)[-1] == "Posso comecar a gerar?"


def test_too_expensive_romantic_uses_flowers():
    brief = Brief(recipient_name="Vanessa", relationship=Relationship.ESPOSA, singer_gender="m")
    joined = " ".join(_too_expensive(brief)).lower()
    assert "buque" in joined and "flores" in joined


def test_too_expensive_non_romantic_avoids_flowers():
    for rel in (Relationship.AMIGO, Relationship.MAE, Relationship.PAI, Relationship.FILHA):
        brief = Brief(recipient_name="Alex", relationship=rel, singer_gender="m")
        joined = " ".join(_too_expensive(brief)).lower()
        assert "buque" not in joined
        assert "flores" not in joined
        assert "pra sempre" in joined   # neutral lasting-value frame instead


def test_is_romantic_flag():
    assert Brief(relationship=Relationship.NAMORADO).is_romantic()
    assert Brief(relationship=Relationship.ESPOSA).is_romantic()
    assert not Brief(relationship=Relationship.AMIGA).is_romantic()
    assert not Brief(relationship=Relationship.MAE).is_romantic()
    assert not Brief().is_romantic()
