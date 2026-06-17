"""Artist -> descriptor mapping + name sanitizer.

Suno blocks requests that NAME real artists. Two jobs here:

1. resolve_style(): when a customer says "no estilo do Henrique e Juliano",
   translate that into a name-free descriptor (genre / voice / BPM /
   instrumentation) that Suno will accept.
2. sanitize()/sanitize_payload(): a last-line scrub that strips any artist name
   that slipped into the style or lyrics before we submit.

Keys in ARTIST_STYLE_MAP are normalized (lowercase, accents stripped).
"""
from __future__ import annotations

import re
import unicodedata

from app.music.schema import KiePayload


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _normalize(s: str) -> str:
    return _strip_accents(s or "").lower().strip()


# Descriptors deliberately contain NO artist names — genre, voice, BPM,
# instrumentation only. Keys are normalized (lowercase, no accents).
ARTIST_STYLE_MAP: dict[str, str] = {
    "henrique e juliano": (
        "modern sertanejo sofrencia, two-part male vocal harmony, BPM 80, "
        "acoustic guitar and viola caipira, accordion, light electronic kick, "
        "heartbreak ballad, emotional but radio-clean"
    ),
    "marilia mendonca": (
        "feminejo sofrencia, powerful expressive female voice, BPM 78, "
        "acoustic guitar, accordion, subtle strings, dramatic heartbreak ballad, "
        "intimate vocal upfront"
    ),
    "bruno e marrone": (
        "romantic sertanejo classico, smooth two-part male harmony, BPM 72, "
        "acoustic guitar, piano, soft strings, crooning love ballad, warm and tender"
    ),
    "jorge e mateus": (
        "sertanejo universitario, upbeat male duo vocals, BPM 96, "
        "acoustic guitar, electric bass, light drums, catchy danceable feel-good love song"
    ),
    "maiara e maraisa": (
        "feminejo duo, twin female vocal harmony, BPM 84, acoustic guitar, "
        "accordion, modern percussion, empowered heartbreak anthem"
    ),
    "zeze di camargo": (
        "classic sertanejo raiz, warm emotive male voice, BPM 76, viola caipira, "
        "acoustic guitar, accordion, nostalgic romantic ballad"
    ),
    "roberto carlos": (
        "romantic brazilian crooner, mellow tender male voice, BPM 70, "
        "grand piano, lush orchestral strings, gentle ballad, timeless romantic feel"
    ),
    "djavan": (
        "sophisticated MPB, smooth agile male voice, BPM 92, nylon acoustic guitar, "
        "jazzy chords, syncopated percussion, bossa-tinged groove, poetic mellow vibe"
    ),
    "tim maia": (
        "brazilian soul and funk, rich powerful male voice, BPM 104, electric piano, "
        "horn section, groovy bass and drums, warm vintage soul feel"
    ),
    "anitta": (
        "brazilian pop, confident bright female voice, BPM 110, electronic production, "
        "punchy synths, funk-influenced beat, danceable radio pop"
    ),
}


def resolve_style(free_text: str) -> str:
    """Map a known artist reference to its descriptor; else pass the text through."""
    norm = _normalize(free_text)
    if not norm:
        return free_text
    if norm in ARTIST_STYLE_MAP:
        return ARTIST_STYLE_MAP[norm]
    # Containment match (e.g. "uma musica tipo henrique e juliano romantica").
    # Longest keys first so the most specific match wins.
    for key in sorted(ARTIST_STYLE_MAP, key=len, reverse=True):
        if key in norm:
            return ARTIST_STYLE_MAP[key]
    return free_text


# Names (and common variants) Suno would reject. Used by sanitize().
ARTIST_NAME_BLOCKLIST: list[str] = [
    "Henrique e Juliano", "Henrique & Juliano",
    "Marilia Mendonca", "Marília Mendonça", "Mendonça", "Mendonca",
    "Bruno e Marrone", "Bruno & Marrone",
    "Jorge e Mateus", "Jorge & Mateus",
    "Maiara e Maraisa", "Maiara & Maraisa",
    "Zeze di Camargo e Luciano", "Zezé di Camargo e Luciano",
    "Zeze di Camargo", "Zezé di Camargo",
    "Roberto Carlos",
    "Djavan",
    "Tim Maia",
    "Anitta",
]


def sanitize(text: str) -> str:
    """Strip any blocklisted artist name from a style/lyrics string."""
    if not text:
        return text
    out = text
    # Longest names first so duos collapse before their fragments.
    for name in sorted(ARTIST_NAME_BLOCKLIST, key=len, reverse=True):
        out = re.sub(re.escape(name), "", out, flags=re.IGNORECASE)
    # Tidy artifacts left by removal.
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"(,\s*){2,}", ", ", out)
    return out.strip(" ,;:-")


def sanitize_payload(p: KiePayload) -> KiePayload:
    """Return a copy of the payload with style + prompt scrubbed of artist names."""
    return p.model_copy(update={"style": sanitize(p.style), "prompt": sanitize(p.prompt)})
