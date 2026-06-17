"""Unit tests for app.music.lyrics (adapted from prompts/02-pop-romantic.json)."""
from __future__ import annotations

from app.music.lyrics import full_lyrics, partial_lyrics

PROMPT = """[Chorus - opens song, bright acoustic strum + ukulele immediately]
Lucas, oito anos e tudo continua
Lucas, voce me faz rir do nada

[Verse 1]
A gente se conheceu sem esperar
Voce dizia que eu era complicada

[Pre-Chorus]
As viagens, os cafes, as brigas bobas
A gente discute mas sempre se entende

[Bridge - softer, just guitar and voice]
Voce e mais solto, eu mais na minha

[Outro - light strum and fade]
Lucas, voce e o meu lugar"""


def test_partial_returns_chorus_and_verse_only():
    out = partial_lyrics(PROMPT)
    # Chorus + first verse present.
    assert "oito anos e tudo continua" in out
    assert "se conheceu sem esperar" in out
    # Pre-chorus / bridge / outro excluded from the teaser.
    assert "As viagens" not in out
    assert "mais solto" not in out
    assert "meu lugar" not in out
    # Production directions stripped — no raw brackets leak.
    assert "[" not in out
    assert "opens song" not in out


def test_full_humanizes_markers_and_drops_production_notes():
    out = full_lyrics(PROMPT)
    # pt-BR section labels.
    assert "Refrão" in out
    assert "Verso" in out
    assert "Pré-refrão" in out
    assert "Ponte" in out
    assert "Final" in out
    # English markers and production notes gone.
    assert "Chorus" not in out
    assert "Verse" not in out
    assert "opens song" not in out
    assert "just guitar" not in out
    assert "[" not in out and "]" not in out
    # Sung content preserved.
    assert "oito anos e tudo continua" in out
    assert "Lucas, voce e o meu lugar" in out
