"""Unit tests for app.music.styles (no network)."""
from __future__ import annotations

from app.music.schema import KiePayload
from app.music.styles import (
    ARTIST_STYLE_MAP,
    resolve_style,
    sanitize,
    sanitize_payload,
)


def test_resolve_known_artist_returns_nameless_descriptor():
    desc = resolve_style("Henrique e Juliano")
    assert desc == ARTIST_STYLE_MAP["henrique e juliano"]
    # The descriptor must not leak the artist name.
    assert "henrique" not in desc.lower()
    assert "juliano" not in desc.lower()


def test_resolve_artist_inside_free_text():
    desc = resolve_style("queria algo no estilo da Marília Mendonça, bem romantico")
    assert desc == ARTIST_STYLE_MAP["marilia mendonca"]


def test_resolve_generic_text_passthrough():
    txt = "romantic acoustic pop, warm female voice, BPM 100"
    assert resolve_style(txt) == txt


def test_sanitize_removes_artist_name():
    out = sanitize("uma musica no estilo da Marília Mendonça bem emocionante")
    low = out.lower()
    assert "marília" not in low and "marilia" not in low
    assert "mendonça" not in low and "mendonca" not in low
    # The rest of the sentence survives.
    assert "emocionante" in low


def test_sanitize_payload_scrubs_style_and_prompt():
    p = KiePayload(
        title="Para Ana",
        style="pop dancante estilo Anitta",
        vocal_gender="f",
        prompt="[Chorus]\nletra feita pra Anitta cantar\nAna voce e tudo",
    )
    sp = sanitize_payload(p)
    assert "anitta" not in sp.style.lower()
    assert "anitta" not in sp.prompt.lower()
    # Untouched fields are preserved; original payload is not mutated.
    assert sp.title == "Para Ana"
    assert "anitta" in p.style.lower()
