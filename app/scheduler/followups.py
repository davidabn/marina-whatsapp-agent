"""Follow-up scheduler.

Runs every minute and asks the runner to send any due follow-ups. The runner
owns the business-hours gate and the per-followup copy; this module is just the
timer. (For multi-instance deploys, run the scheduler in exactly one replica.)
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.graph import runner

log = logging.getLogger("marina.scheduler")
_scheduler: AsyncIOScheduler | None = None


async def _tick() -> None:
    try:
        await runner.run_due_followups()
    except Exception:  # noqa: BLE001
        log.exception("followup tick failed")


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone=settings.tz)
    _scheduler.add_job(_tick, "interval", minutes=1, id="due-followups",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("followup scheduler started (tz=%s)", settings.tz)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
