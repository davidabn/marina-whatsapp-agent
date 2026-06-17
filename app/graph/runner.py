"""Runner — the API the web layer (main.py / webhooks / scheduler) calls.

Responsibilities the GRAPH deliberately does NOT own:
- WhatsApp send (flush `state['outbound']` -> Evolution).
- inbound dedupe, contact/conversation resolution, STT, message logging.
- per-jid serialization + debounce.
- business-state persistence (conversations row) and follow-up scheduling.

Three entrypoints drive one graph invocation each:
- handle_inbound(inbound)        user turn
- on_generation_complete(task)   KIE webhook  -> preview node
- on_payment_event(mp_payment)   pix webhook  -> deliver node
plus run_due_followups() for the scheduler.

The graph is invoked through the single reusable `_invoke` helper, which
preserves the persisted `extra` bag (regen flags, lyrics_prompt, conversation_id)
while injecting the one-shot event and resetting `outbound`.

Checkpointer: an AsyncPostgresSaver (langgraph-checkpoint-postgres) over
settings.supabase_db_url, opened once at setup() and kept for the app lifetime.
thread_id == wa_jid.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

try:  # zoneinfo is stdlib 3.9+; fall back gracefully if the tz db is missing
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from app.config import settings
from app.db import repo
from app.evolution.client import EvolutionClient
from app.evolution.types import InboundMessage
from app.graph.build import build_graph
from app.graph.state import Stage
from app.media import stt
from app.music import kie
from app.payments.mercadopago import MercadoPagoProvider
from app.utils.debounce import DebounceBuffer
from app.utils.locks import contact_lock

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Module-level singletons
# --------------------------------------------------------------------------- #
_graph = None
_saver = None
_saver_cm = None
_evo: Optional[EvolutionClient] = None
_mp: Optional[MercadoPagoProvider] = None
_debounce = DebounceBuffer(settings.debounce_seconds)


def get_graph():
    if _graph is None:
        raise RuntimeError("graph not built; call setup() first")
    return _graph


def get_evolution() -> EvolutionClient:
    global _evo
    if _evo is None:
        _evo = EvolutionClient()
    return _evo


def get_mp() -> MercadoPagoProvider:
    global _mp
    if _mp is None:
        _mp = MercadoPagoProvider()
    return _mp


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def setup() -> None:
    """Open the DB pool + checkpointer, compile the graph, wire singletons."""
    global _graph, _saver, _saver_cm, _evo, _mp

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    _saver_cm = AsyncPostgresSaver.from_conn_string(settings.supabase_db_url)
    _saver = await _saver_cm.__aenter__()
    try:
        await _saver.setup()  # idempotent migrations; tolerate already-applied
    except Exception:  # noqa: BLE001
        logger.warning("AsyncPostgresSaver.setup() raised (likely already set up)", exc_info=True)

    _graph = build_graph(_saver)
    _evo = EvolutionClient()
    _mp = MercadoPagoProvider()
    logger.info("Marina graph runner ready")


async def teardown() -> None:
    global _graph, _saver, _saver_cm, _evo
    if _saver_cm is not None:
        try:
            await _saver_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.warning("checkpointer close failed", exc_info=True)
        _saver_cm = None
        _saver = None
    if _evo is not None:
        await _evo.aclose()
        _evo = None
    _graph = None


# --------------------------------------------------------------------------- #
# Reset a contact (testing): wipe BOTH the business rows and the checkpointer
# --------------------------------------------------------------------------- #
async def reset_thread(jid: str) -> bool:
    """Drop the LangGraph conversation memory for a thread (= wa_jid).

    Removes checkpoints/checkpoint_writes/checkpoint_blobs for this thread_id.
    Tolerates a missing/empty thread. Returns True if the delete ran.
    """
    if _saver is None:
        return False
    try:
        await _saver.adelete_thread(jid)
        return True
    except Exception:  # noqa: BLE001 — nonexistent thread is fine
        logger.warning("reset_thread failed for %s", jid, exc_info=True)
        return False


async def reset_contact(needle: str) -> dict:
    """Make a number look brand-new: delete its Supabase rows (cascade) and its
    LangGraph checkpoint(s). `needle` may be a phone (digits) or a full wa_jid.
    """
    matched: list[str] = []
    supabase_deleted = 0
    threads_cleared = 0
    try:
        rows = await repo.find_contacts(needle)
    except Exception:  # noqa: BLE001
        logger.exception("find_contacts failed")
        rows = []

    for row in rows:
        jid = row.get("wa_jid")
        if not jid:
            continue
        matched.append(jid)
        try:
            supabase_deleted += await repo.delete_contact(jid)
        except Exception:  # noqa: BLE001
            logger.exception("delete_contact failed for %s", jid)
        if await reset_thread(jid):
            threads_cleared += 1

    # No contact row matched — still try to clear a stray checkpoint by jid.
    if not matched:
        digits = "".join(ch for ch in needle if ch.isdigit())
        jid = needle if "@" in needle else (f"{digits}@s.whatsapp.net" if digits else "")
        if jid and await reset_thread(jid):
            threads_cleared += 1
            matched.append(jid)

    return {
        "matched": matched,
        "supabase_deleted": supabase_deleted,
        "threads_cleared": threads_cleared,
    }


# --------------------------------------------------------------------------- #
# Core graph invocation (reused by all entrypoints)
# --------------------------------------------------------------------------- #
async def _invoke(
    jid: str,
    *,
    inbound_text: Optional[str] = None,
    event: Optional[str] = None,
    conversation_id: Optional[str] = None,
    extra_updates: Optional[dict] = None,
    channels: Optional[dict] = None,
) -> dict:
    graph = get_graph()
    config = {"configurable": {"thread_id": jid}}

    # Preserve the persisted extra bag; drop any stale one-shot event.
    try:
        snap = await graph.aget_state(config)
        existing = dict((snap.values if snap else {}) or {})
    except Exception:  # noqa: BLE001 — fresh thread / no checkpoint yet
        existing = {}
    extra = dict(existing.get("extra") or {})
    extra.pop("event", None)
    if conversation_id:
        extra["conversation_id"] = str(conversation_id)
    if event:
        extra["event"] = event
    if extra_updates:
        extra.update(extra_updates)

    inp: dict[str, Any] = {"outbound": [], "extra": extra, "wa_jid": jid}
    if inbound_text is not None:
        inp["inbound_text"] = inbound_text
        inp["messages"] = [("human", inbound_text)]
    if channels:
        inp.update(channels)

    return await graph.ainvoke(inp, config)


# --------------------------------------------------------------------------- #
# Outbound flush
# --------------------------------------------------------------------------- #
async def _flush(
    number: str,
    result: dict,
    *,
    conversation_id: Optional[str],
    contact_id: Optional[str] = None,
) -> None:
    outbound = result.get("outbound") or []
    evo = get_evolution()

    # Send in order, batching consecutive text bubbles into one human-paced run.
    pending: list[str] = []
    for item in outbound:
        if item.get("kind") == "text":
            pending.append(item.get("text") or "")
        else:
            if pending:
                await evo.send_text_sequence(number, pending)
                pending = []
            if item.get("kind") == "audio" and item.get("url"):
                await evo.send_audio(number, item["url"])
    if pending:
        await evo.send_text_sequence(number, pending)

    # Log outbound + persist business state.
    for item in outbound:
        try:
            if item.get("kind") == "text":
                await repo.log_message(
                    conversation_id=conversation_id, contact_id=contact_id,
                    direction="out", kind="text", content=item.get("text"),
                )
            else:
                await repo.log_message(
                    conversation_id=conversation_id, contact_id=contact_id,
                    direction="out", kind="audio",
                    content=item.get("caption"), media_url=item.get("url"),
                )
        except Exception:  # noqa: BLE001
            logger.exception("log outbound failed")

    if conversation_id:
        try:
            await repo.update_conversation(
                conversation_id,
                stage=result.get("stage"),
                brief=result.get("brief"),
                chosen_variant=result.get("chosen_variant"),
                regen_count=result.get("regen_count"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_conversation failed")

    if result.get("needs_human") and contact_id:
        try:
            await repo.set_needs_human(contact_id, True)
        except Exception:  # noqa: BLE001
            logger.exception("set_needs_human failed")


# --------------------------------------------------------------------------- #
# Entry: inbound user message
# --------------------------------------------------------------------------- #
async def handle_inbound(inbound: InboundMessage) -> None:
    if not inbound.is_actionable:
        return
    try:
        if await repo.message_exists(inbound.message_id):
            return
    except Exception:  # noqa: BLE001
        logger.exception("message_exists check failed")

    # Debug-only: a secret word from the tester wipes this chat's history so the
    # next "oi" looks like a brand-new first contact. Runs before contact/logging
    # so the reset command itself doesn't recreate the row. Disabled when unset.
    if settings.debug_reset_word and (inbound.text or "").strip().lower() == \
            settings.debug_reset_word.strip().lower():
        result = await reset_contact(inbound.jid)
        logger.info("debug reset for %s: %s", inbound.jid, result)
        try:
            await get_evolution().send_text(
                inbound.phone, "pronto, histórico zerado 🧹 manda um oi pra recomeçar 💛"
            )
        except Exception:  # noqa: BLE001
            logger.exception("reset confirm send failed")
        return

    contact = await repo.get_or_create_contact(
        inbound.jid, push_name=inbound.push_name, phone=inbound.phone
    )
    conv = await repo.get_or_create_conversation(contact["id"])
    conv_id = str(conv["id"])
    contact_id = str(contact["id"])

    text = inbound.text or ""
    if inbound.kind == "audio":
        try:
            data = await get_evolution().fetch_media(inbound)
            text = await stt.transcribe(
                data, mimetype=inbound.mimetype or "audio/ogg", filename="audio.ogg"
            )
        except Exception:  # noqa: BLE001
            logger.exception("inbound audio transcription failed")

    try:
        await repo.log_message(
            conversation_id=conv_id, contact_id=contact_id, direction="in",
            kind=inbound.kind, content=text, wa_message_id=inbound.message_id,
            raw=inbound.raw,
        )
    except Exception:  # noqa: BLE001
        logger.exception("log inbound failed")

    # Debounce OUTSIDE the lock so co-arriving bubbles coalesce into one turn.
    joined = await _debounce.collect(inbound.jid, text)
    if joined is None:
        return

    async with contact_lock(inbound.jid):
        # A fresh inbound means the lead re-engaged: drop any pending follow-ups.
        try:
            await repo.cancel_pending_followups(conv_id)
        except Exception:  # noqa: BLE001
            logger.exception("cancel_pending_followups failed")

        result = await _invoke(inbound.jid, inbound_text=joined, conversation_id=conv_id)
        await _flush(inbound.phone, result, conversation_id=conv_id, contact_id=contact_id)
        await _maybe_schedule_followups(conv_id, inbound.jid, result)


# --------------------------------------------------------------------------- #
# Entry: KIE generation finished
# --------------------------------------------------------------------------- #
async def on_generation_complete(task_id: str) -> None:
    gen = await repo.get_generation_by_task(task_id)
    if not gen:
        logger.warning("on_generation_complete: unknown task %s", task_id)
        return
    conv_id = str(gen.get("conversation_id") or "")
    jid = await repo.get_jid_by_conversation(conv_id) if conv_id else None
    if not jid:
        logger.warning("on_generation_complete: no jid for task %s", task_id)
        return

    # Confirm + collect variants via record-info (the webhook is just a ping).
    res = await kie.fetch_result(task_id)
    if not res.succeeded:
        try:
            await repo.update_generation(task_id, status=res.status, error=res.error)
        except Exception:  # noqa: BLE001
            logger.exception("update_generation (failure) failed")
        return
    variants = [v.model_dump() for v in res.variants]
    try:
        await repo.update_generation(task_id, status="SUCCESS", variants=variants)
    except Exception:  # noqa: BLE001
        logger.exception("update_generation (success) failed")

    async with contact_lock(jid):
        result = await _invoke(
            jid,
            event="generation_done",
            conversation_id=conv_id,
            channels={"variants": variants, "kie_task_id": task_id},
        )
        await _flush(_phone(jid), result, conversation_id=conv_id)


# --------------------------------------------------------------------------- #
# Entry: payment notification
# --------------------------------------------------------------------------- #
async def on_payment_event(mp_payment_id: str) -> None:
    order = await repo.get_order_by_mp_payment(mp_payment_id)
    if not order:
        logger.warning("on_payment_event: unknown payment %s", mp_payment_id)
        return
    if (order.get("status") or "").lower() == "paid":
        return  # idempotent: already delivered

    charge = await get_mp().get_payment(mp_payment_id)
    if not await get_mp().is_approved(charge.status):
        return

    paid = await repo.mark_order_paid(mp_payment_id)
    if not paid:
        return  # lost the race / already paid elsewhere

    conv_id = str(order.get("conversation_id") or "")
    jid = await repo.get_jid_by_conversation(conv_id) if conv_id else None
    if not jid:
        logger.warning("on_payment_event: no jid for payment %s", mp_payment_id)
        return

    async with contact_lock(jid):
        result = await _invoke(
            jid, event="payment_done", conversation_id=conv_id, channels={"paid": True}
        )
        await _flush(_phone(jid), result, conversation_id=conv_id)
        try:
            await repo.cancel_pending_followups(conv_id)
        except Exception:  # noqa: BLE001
            logger.exception("cancel_pending_followups (post-pay) failed")


# --------------------------------------------------------------------------- #
# Scheduler: due follow-ups
# --------------------------------------------------------------------------- #
async def run_due_followups() -> None:
    now = _now()
    if not (settings.business_hours_start <= now.hour < settings.business_hours_end):
        return  # respect business hours

    due = await repo.due_followups(now)
    for fup in due:
        conv_id = str(fup.get("conversation_id") or "")
        # kind is clean: one of postsale, cold_1, cold_2, cold_3.
        kind = fup.get("kind") or "postsale"
        jid = await repo.get_jid_by_conversation(conv_id) if conv_id else None
        if not jid:
            continue
        try:
            async with contact_lock(jid):
                result = await _invoke(
                    jid, event="followup", conversation_id=conv_id,
                    extra_updates={"followup_kind": kind},
                )
                await _flush(_phone(jid), result, conversation_id=conv_id)
            await repo.mark_followup_sent(str(fup.get("id")))
        except Exception:  # noqa: BLE001
            logger.exception("followup %s failed", fup.get("id"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _phone(jid: str) -> str:
    return (jid or "").split("@", 1)[0]


def _now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(settings.tz))
        except Exception:  # noqa: BLE001
            pass
    return datetime.now()


async def _maybe_schedule_followups(conv_id: str, jid: str, result: dict) -> None:
    """When a pix was just sent (entered PIX_WAIT), arm the cold follow-ups."""
    if result.get("stage") != Stage.PIX_WAIT.value:
        return
    base = _now()
    schedule = [
        ("cold_1", timedelta(hours=3)),
        ("cold_2", timedelta(hours=20)),
        ("cold_3", timedelta(hours=50)),
    ]
    for kind, delay in schedule:
        try:
            await repo.schedule_followup(conv_id, kind, base + delay)
        except Exception:  # noqa: BLE001
            logger.exception("schedule_followup %s failed", kind)
