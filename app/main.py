"""Marina WhatsApp Agent — FastAPI entrypoint.

Lifespan brings up the runner (DB pool, LangGraph + checkpointer, Evolution +
Mercado Pago clients) and the follow-up scheduler. Webhooks are thin: they parse
+ verify and hand off to the runner in the background.
"""
from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.graph import runner
from app.scheduler.followups import start_scheduler, stop_scheduler
from app.webhooks import evolution as evolution_webhook
from app.webhooks import kie as kie_webhook
from app.webhooks import payments as payments_webhook

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("marina")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting marina agent…")
    await runner.setup()
    start_scheduler()
    try:
        yield
    finally:
        log.info("shutting down…")
        stop_scheduler()
        await runner.teardown()


app = FastAPI(title="Marina WhatsApp Agent", lifespan=lifespan)
app.include_router(evolution_webhook.router)
app.include_router(kie_webhook.router)
app.include_router(payments_webhook.router)


@app.get("/health")
async def health():
    ffmpeg_ok = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    from app.db import repo
    db_ok = await repo.healthcheck()
    healthy = ffmpeg_ok and db_ok
    return {"status": "ok" if healthy else "degraded", "ffmpeg": ffmpeg_ok, "db": db_ok}


@app.get("/")
async def root():
    return {"service": "marina-whatsapp-agent"}


@app.post("/tasks/run-due-followups")
async def run_due_followups():
    """Manual / cron trigger for the follow-up sweep (alternative to the in-process scheduler)."""
    await runner.run_due_followups()
    return {"ok": True}


@app.post("/admin/reset-contact")
async def reset_contact(request: Request):
    """Wipe a contact's history (Supabase rows + LangGraph checkpoint) so the
    number looks brand-new. Guarded by ADMIN_TOKEN. Body/query: {phone|wa_jid}.
    """
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    if not settings.admin_token or token != settings.admin_token:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    needle = (
        body.get("phone") or body.get("wa_jid")
        or request.query_params.get("phone") or ""
    ).strip()
    if not needle:
        return JSONResponse({"ok": False, "error": "missing phone/wa_jid"}, status_code=400)
    return {"ok": True, **await runner.reset_contact(needle)}
