"""Async KIE.ai / Suno client — a faithful port of music-pipeline/*.sh.

Stateless and idempotent: every call opens its own httpx client and carries the
Bearer key. The bash pipeline (generate.sh + wait.sh + lib.sh) is reproduced here:

    generate.sh -> submit()        POST /api/v1/generate            -> data.taskId
    wait.sh     -> fetch_result()  GET  /api/v1/generate/record-info -> data.status
                   poll()          loop fetch_result() with backoff
                   (curl -L)       download()                        -> audio bytes

Used by both the KIE webhook handler (fetch_result) and the safety-net poller
(poll).
"""
from __future__ import annotations

import asyncio

import httpx

from app.config import settings
from app.music.schema import GenerationResult, KiePayload, Variant

_GENERATE_PATH = "/api/v1/generate"
_RECORD_PATH = "/api/v1/generate/record-info"

# Statuses recognized exactly as wait.sh does.
SUCCESS_STATUS = "SUCCESS"
IN_PROGRESS_STATUSES = frozenset({"PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS"})


def _headers(json_body: bool = False) -> dict[str, str]:
    h = {"Authorization": f"Bearer {settings.kie_api_key}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _is_failure(status: str) -> bool:
    """Terminal failure: any status containing FAIL / ERROR / SENSITIVE (wait.sh)."""
    s = (status or "").upper()
    return "FAIL" in s or "ERROR" in s or "SENSITIVE" in s


def _extract_variants(data: dict) -> list[Variant]:
    """Pull audio variants from data.response.sunoData[] (with wait.sh fallbacks)."""
    response = data.get("response") or {}
    arr = (
        response.get("sunoData")
        or data.get("sunoData")
        or data.get("audio")
        or []
    )
    variants: list[Variant] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        vid = str(item.get("id") or "")
        audio_url = item.get("audioUrl") or item.get("audio_url") or ""
        title = item.get("title") or item.get("id") or ""
        image_url = item.get("imageUrl") or item.get("image_url") or ""
        if not audio_url:
            continue
        variants.append(
            Variant(id=vid, audio_url=audio_url, title=str(title), image_url=str(image_url))
        )
    return variants


async def submit(payload: KiePayload) -> str:
    """POST the generation request; return data.taskId. Raises on code != 200."""
    body = payload.to_kie_json()
    if settings.kie_callback_url:
        body["callBackUrl"] = settings.kie_callback_url

    async with httpx.AsyncClient(base_url=settings.kie_base_url, timeout=60.0) as client:
        resp = await client.post(_GENERATE_PATH, json=body, headers=_headers(json_body=True))
        resp.raise_for_status()
        payload_json = resp.json()

    code = payload_json.get("code")
    if code != 200:
        msg = payload_json.get("msg") or payload_json.get("message") or "unknown error"
        raise RuntimeError(f"KIE submit failed (code={code}): {msg}")

    task_id = ((payload_json.get("data") or {}).get("taskId")) or ""
    if not task_id:
        raise RuntimeError(f"KIE submit returned no taskId: {payload_json}")
    return str(task_id)


async def fetch_result(task_id: str) -> GenerationResult:
    """One GET of record-info; map status and (on SUCCESS) extract variants."""
    async with httpx.AsyncClient(base_url=settings.kie_base_url, timeout=30.0) as client:
        resp = await client.get(
            _RECORD_PATH, params={"taskId": task_id}, headers=_headers()
        )
        resp.raise_for_status()
        body = resp.json()

    data = body.get("data") or {}
    status = data.get("status") or "UNKNOWN"

    variants: list[Variant] = []
    error = None
    if status == SUCCESS_STATUS:
        variants = _extract_variants(data)
    elif _is_failure(status):
        error = (
            data.get("errorMessage")
            or data.get("error")
            or body.get("msg")
            or status
        )

    return GenerationResult(
        task_id=task_id, status=status, variants=variants, error=error
    )


async def poll(
    task_id: str,
    *,
    interval: float = settings.kie_poll_interval,
    max_attempts: int = settings.kie_max_attempts,
) -> GenerationResult:
    """Safety-net poller: loop fetch_result() until terminal or attempts exhausted."""
    last: GenerationResult | None = None
    for attempt in range(max_attempts):
        last = await fetch_result(task_id)
        if last.status == SUCCESS_STATUS:
            return last
        if _is_failure(last.status):
            return last
        if attempt < max_attempts - 1:
            await asyncio.sleep(interval)

    return GenerationResult(
        task_id=task_id,
        status="TIMEOUT",
        variants=last.variants if last else [],
        error=f"polling exhausted after {max_attempts} attempts",
    )


async def download(url: str) -> bytes:
    """GET the media bytes (curl -L equivalent: follow redirects). Audio or image."""
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content
