"""Tiny URL shortener (is.gd) for the InfinitePay checkout link.

The hosted checkout URL carries a long `lenc` token, which reads as spammy in a
warm WhatsApp chat. is.gd's free `create.php` endpoint returns the short URL as
plain text. This is best-effort: any error/timeout (or a non-https result) falls
back to the original URL, so a shortener outage never blocks a sale.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_IS_GD = "https://is.gd/create.php"


async def shorten(url: str, *, timeout: float = 5.0) -> str:
    """Return a shortened URL, or the original `url` on any failure."""
    if not url:
        return url
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(
                _IS_GD, params={"format": "simple", "url": url}
            )
        resp.raise_for_status()
        short = (resp.text or "").strip()
        if short.startswith("https://") or short.startswith("http://"):
            return short
        logger.warning("shorten: unexpected is.gd response %r; using original", short[:120])
    except Exception:  # noqa: BLE001 — never block checkout on the shortener
        logger.warning("shorten failed; using original url", exc_info=True)
    return url
