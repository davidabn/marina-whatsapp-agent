"""Format the authored `prompt` (lyrics + production markers) for humans.

The KIE `prompt` field carries the full lyric with English section markers and
inline production directions, e.g.:

    [Chorus - opens song, bright acoustic strum + ukulele immediately]
    Lucas, oito anos e tudo continua
    ...

Two views are produced from it:

- partial_lyrics(): a pre-payment teaser — first chorus + first verse only,
  production directions stripped.
- full_lyrics(): the whole lyric, section markers humanized to pt-BR and
  production notes removed, ready to show the customer after payment.
"""
from __future__ import annotations

import re

_HEADER_RE = re.compile(r"^\s*\[(?P<tag>[^\]]*)\]\s*$")
_INLINE_BRACKETS_RE = re.compile(r"\[[^\]]*\]")


def _parse_sections(prompt: str) -> list[tuple[str, list[str]]]:
    """Split the prompt into (header_tag, body_lines) sections."""
    sections: list[tuple[str | None, list[str]]] = []
    cur_tag: str | None = None
    cur_lines: list[str] = []
    started = False

    for line in (prompt or "").splitlines():
        m = _HEADER_RE.match(line)
        if m:
            if started:
                sections.append((cur_tag, cur_lines))
            cur_tag = m.group("tag")
            cur_lines = []
            started = True
        elif started:
            cur_lines.append(line)
    if started:
        sections.append((cur_tag, cur_lines))

    return [(t, lines) for (t, lines) in sections if t is not None]


def _clean_lines(lines: list[str]) -> list[str]:
    """Drop inline production notes and blank lines; keep the sung lines."""
    out: list[str] = []
    for line in lines:
        cleaned = _INLINE_BRACKETS_RE.sub("", line).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _humanize(tag: str) -> str:
    """Map an English section tag to a pt-BR label, dropping production notes."""
    h = tag.strip().lower()
    if h.startswith("pre-chorus") or h.startswith("pre chorus") or "pré" in h:
        return "Pré-refrão"
    if h.startswith("chorus"):
        return "Refrão"
    if h.startswith("verse"):
        return "Verso"
    if h.startswith("bridge"):
        return "Ponte"
    if h.startswith("outro"):
        return "Final"
    if h.startswith("intro"):
        return "Início"
    # Unknown marker: keep the leading keyword, capitalized, note dropped.
    head = re.split(r"[-,]", tag, maxsplit=1)[0].strip()
    return head.title() or "Trecho"


def partial_lyrics(prompt: str) -> str:
    """Teaser: first [Chorus] block + first [Verse]/[Verse 1] block only."""
    chorus: list[str] | None = None
    verse: list[str] | None = None

    for tag, lines in _parse_sections(prompt):
        t = tag.strip().lower()
        if chorus is None and t.startswith("chorus"):
            chorus = _clean_lines(lines)
        elif verse is None and t.startswith("verse"):
            verse = _clean_lines(lines)
        if chorus is not None and verse is not None:
            break

    parts: list[str] = []
    if chorus:
        parts.append("\n".join(chorus))
    if verse:
        parts.append("\n".join(verse))
    return "\n\n".join(parts)


def full_lyrics(prompt: str) -> str:
    """Whole lyric with pt-BR section labels and production notes removed."""
    blocks: list[str] = []
    for tag, lines in _parse_sections(prompt):
        body = _clean_lines(lines)
        if not body:
            continue
        label = _humanize(tag)
        blocks.append(label + "\n" + "\n".join(body))
    return "\n\n".join(blocks)
