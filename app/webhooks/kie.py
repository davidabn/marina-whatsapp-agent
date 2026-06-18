"""KIE.ai generation callback.

KIE POSTs here when a song finishes. We extract the taskId and resume the graph
(preview node). Idempotency is handled in the runner / generations table.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from app.graph import runner
from app.utils.background import spawn

log = logging.getLogger("marina.webhook.kie")
router = APIRouter()


def _extract_task_id(body: dict[str, Any]) -> str | None:
    # KIE payloads vary: {taskId} | {data:{taskId}} | {data:{task_id}}
    for path in (("taskId",), ("task_id",), ("data", "taskId"), ("data", "task_id")):
        cur: Any = body
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur:
            return cur
    return None


@router.post("/webhooks/kie")
async def kie_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": True, "skipped": "no json"}

    task_id = _extract_task_id(body)
    if not task_id:
        log.warning("kie webhook without taskId: %s", body)
        return {"ok": True, "skipped": "no taskId"}

    spawn(runner.on_generation_complete(task_id), name=f"kie:{task_id}")
    return {"ok": True}
