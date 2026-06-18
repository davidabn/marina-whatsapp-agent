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
_MP4_GENERATE_PATH = "/api/v1/mp4/generate"
_MP4_RECORD_PATH = "/api/v1/mp4/record-info"

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
        if not audio_url:
            continue
        variants.append(Variant(id=vid, audio_url=audio_url, title=str(title)))
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
    """GET the media bytes (curl -L equivalent: follow redirects). Audio or video."""
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


# --------------------------------------------------------------------------- #
# Music video (MP4 visualizer) — POST /api/v1/mp4/generate, result via callback
# --------------------------------------------------------------------------- #
async def submit_mp4(task_id: str, audio_id: str, *, call_back_url: str | None = None) -> str:
    """Submit an MP4 (visualizer) job for one audio variant of a finished song.

    POST /api/v1/mp4/generate {taskId, audioId, callBackUrl} -> data.taskId (a NEW
    id for the video task). KIE later POSTs the finished `data.video_url` to the
    callback. Raises on a non-200 code.
    """
    body: dict = {"taskId": task_id, "audioId": audio_id}
    cb = call_back_url or settings.kie_callback_url
    if cb:
        body["callBackUrl"] = cb
    async with httpx.AsyncClient(base_url=settings.kie_base_url, timeout=60.0) as client:
        resp = await client.post(_MP4_GENERATE_PATH, json=body, headers=_headers(json_body=True))
        resp.raise_for_status()
        pj = resp.json()
    code = pj.get("code")
    if code != 200:
        msg = pj.get("msg") or pj.get("message") or "unknown error"
        raise RuntimeError(f"KIE mp4 submit failed (code={code}): {msg}")
    mp4_task = ((pj.get("data") or {}).get("taskId")) or ""
    if not mp4_task:
        raise RuntimeError(f"KIE mp4 submit returned no taskId: {pj}")
    return str(mp4_task)


def extract_video_url(data: dict) -> str:
    """Best-effort pull of the MP4 URL from a callback/record-info `data` block."""
    response = data.get("response") or {}
    return str(
        data.get("video_url") or data.get("videoUrl")
        or response.get("videoUrl") or response.get("video_url")
        or response.get("resultUrl") or ""
    )


async def fetch_mp4(mp4_task_id: str) -> str:
    """One GET of the mp4 record-info; return the video URL if ready, else ''."""
    async with httpx.AsyncClient(base_url=settings.kie_base_url, timeout=30.0) as client:
        resp = await client.get(_MP4_RECORD_PATH, params={"taskId": mp4_task_id}, headers=_headers())
        resp.raise_for_status()
        body = resp.json()
    return extract_video_url(body.get("data") or {})


async def poll_mp4(
    mp4_task_id: str,
    *,
    interval: float = settings.kie_poll_interval,
    max_attempts: int = settings.kie_max_attempts,
) -> str:
    """Best-effort safety poller for the MP4 url. '' if it never becomes ready
    (the documented path is the callback; record-info may be unavailable)."""
    for attempt in range(max_attempts):
        try:
            url = await fetch_mp4(mp4_task_id)
            if url:
                return url
        except Exception:  # noqa: BLE001 — tolerate; the callback is the primary path
            pass
        if attempt < max_attempts - 1:
            await asyncio.sleep(interval)
    return ""
