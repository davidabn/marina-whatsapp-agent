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

import asyncio
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
from app.graph.nodes import recipient_pronoun
from app.graph.state import Brief, Stage
from app.media import storage, stt
from app.music import kie, lyrics
from app.music import preview as preview_mod
from app.payments.base import PaymentProvider
from app.payments.infinitepay import InfinityPayProvider
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
_payment: Optional[PaymentProvider] = None
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


def get_payment_provider() -> PaymentProvider:
    """The active checkout/charge provider (settings.payment_provider).

    Defaults to InfinitePay; "mercadopago" selects the legacy PIX path.
    """
    global _payment
    if _payment is None:
        if settings.payment_provider == "mercadopago":
            _payment = get_mp()
        else:
            _payment = InfinityPayProvider()
    return _payment


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def setup() -> None:
    """Open the DB pool + checkpointer, compile the graph, wire singletons."""
    global _graph, _saver, _saver_cm, _evo, _mp, _payment

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
    _payment = None  # lazily built by get_payment_provider() per settings
    logger.info("Marina graph runner ready (payments: %s)", settings.payment_provider)


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
            elif item.get("kind") == "video" and item.get("url"):
                await evo.send_media(
                    number, item["url"], mediatype="video", mimetype="video/mp4",
                    caption=item.get("caption"),
                )
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
                    direction="out", kind=item.get("kind") or "audio",
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
    # Dedupe: the KIE webhook and the safety-net poller can both fire. Once the
    # preview has been delivered (preview_url set), there is nothing to do.
    if gen.get("preview_url"):
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
        # Re-check under the lock: the webhook and poller race to get here, and
        # the preview node stamps preview_url inside the lock. Whoever lands
        # second sees it and bails, so the preview is sent exactly once.
        latest = await repo.get_generation_by_task(task_id)
        if latest and latest.get("preview_url"):
            return
        result = await _invoke(
            jid,
            event="generation_done",
            conversation_id=conv_id,
            channels={"variants": variants, "kie_task_id": task_id},
        )
        await _flush(_phone(jid), result, conversation_id=conv_id)


async def poll_generation(task_id: str) -> None:
    """Safety-net poller (spawned by the generate node).

    KIE's terminal "complete" callback is occasionally dropped, which would leave
    the conversation parked at GENERATION_WAIT forever. We poll record-info and,
    on SUCCESS, drive the preview ourselves via on_generation_complete (which is
    idempotent against the webhook). No-op if the webhook already delivered.
    """
    try:
        gen = await repo.get_generation_by_task(task_id)
        if gen and gen.get("preview_url"):
            return  # webhook already delivered
        res = await kie.poll(task_id)
        if not res.succeeded:
            logger.info("poll_generation: %s ended %s (webhook may still arrive)",
                        task_id, res.status)
            return
        await on_generation_complete(task_id)
    except Exception:  # noqa: BLE001 — background safety net must never crash
        logger.exception("poll_generation failed for %s", task_id)


# --------------------------------------------------------------------------- #
# Background: render + send the paid full song (spawned by the deliver node)
# --------------------------------------------------------------------------- #
async def render_and_send_full_video(
    jid: str,
    conv_id: Optional[str],
    chosen: dict,
    kie_task_id: Optional[str],
    brief_dict: Optional[dict] = None,
    lyrics_prompt: str = "",
) -> None:
    """Render the paid full song as a video and send it, then the closing bubbles.

    Primary deliverable: Suno's official MP4 visualizer (KIE renders it ~2 min,
    off our server). Fallbacks, in order: a local 9:16 ffmpeg video (cover + full
    audio), then a full-audio note. The render runs OUTSIDE the contact lock so a
    2-minute KIE render never blocks the user; only the send + bookkeeping take
    the lock. Followed by the celebration / lyrics-offer / UGC-seed bubbles.
    """
    audio_url = chosen.get("audio_url") or chosen.get("audioUrl") or ""
    image_url = chosen.get("image_url") or chosen.get("imageUrl") or ""
    audio_id = chosen.get("id") or ""
    anon = conv_id or jid or "anon"

    full_url: Optional[str] = None
    full_audio_url: Optional[str] = None

    # 1) Preferred: the official Suno visualizer (animated 9:16, off our server).
    if kie_task_id and audio_id:
        try:
            mp4_task = await kie.submit_mp4(kie_task_id, audio_id)
            video_url = await kie.poll_mp4(mp4_task)
            if video_url:
                data = await kie.download(video_url)  # temp host -> re-host on Supabase
                path = storage.build_path(anon, prefix="full", ext="mp4")
                full_url = await storage.upload(path, data, "video/mp4")
        except Exception:  # noqa: BLE001
            logger.exception("suno visualizer failed; falling back to local render")
            full_url = None

    # 2) Fallback: local ffmpeg 9:16 video, then a full-audio note.
    if not full_url and audio_url:
        try:
            audio_data = await kie.download(audio_url)
            if image_url:
                try:
                    image_data = await kie.download(image_url)
                    clip = await asyncio.to_thread(
                        preview_mod.make_full_video, audio_data, image_data
                    )
                    path = storage.build_path(anon, prefix="full", ext="mp4")
                    full_url = await storage.upload(path, clip, "video/mp4")
                except Exception:  # noqa: BLE001
                    logger.exception("local full video build failed; using audio")
                    full_url = None
            if not full_url:
                path = storage.build_path(anon, prefix="full", ext="mp3")
                full_audio_url = await storage.upload(path, audio_data, "audio/mpeg")
        except Exception:  # noqa: BLE001
            logger.exception("full-audio fallback failed")

    brief = Brief(**(brief_dict or {}))
    name = brief.recipient_name or "essa pessoa"
    rec = recipient_pronoun(brief, unknown=name)

    outbound: list[dict] = []
    if full_url:
        outbound.append({"kind": "video", "url": full_url, "caption": "A musica completa 🎶"})
    elif full_audio_url:
        outbound.append({"kind": "audio", "url": full_audio_url, "caption": "A musica completa 🎶"})
    else:
        outbound.append({
            "kind": "text",
            "text": "Deu uma engasgada aqui pra montar o video 😅 ja te resolvo e te mando, ta? 💛",
        })

    if full_url or full_audio_url:
        outbound.append({
            "kind": "text",
            "text": f"Aqui ela, completinha, pra ti e pro {name} pra sempre 🎶",
        })
        if lyrics_prompt and lyrics.full_lyrics(lyrics_prompt):
            outbound.append({
                "kind": "text",
                "text": "Se quiser a letra escrita pra acompanhar, e so me pedir 💛",
            })
        outbound.append({
            "kind": "text",
            "text": (
                f"Manda pra {rec} de um jeito especial 💛 grava a reacao se conseguir, "
                "e o melhor presente que tu vai ganhar de volta 🥹"
            ),
        })

    delivered_url = full_url or full_audio_url
    async with contact_lock(jid):
        await _flush(_phone(jid), {"outbound": outbound}, conversation_id=conv_id)
        if kie_task_id and delivered_url:
            try:
                await repo.update_generation(
                    kie_task_id, full_url=delivered_url, completed=True
                )
            except Exception:  # noqa: BLE001
                logger.exception("update_generation (full delivery) failed")


# --------------------------------------------------------------------------- #
# Entry: payment notification
# --------------------------------------------------------------------------- #
async def _deliver_for_order(order: dict) -> None:
    """Shared post-payment tail: resume the graph at `deliver` and clean up.

    Callers must have already flipped the order to `paid` (so this runs exactly
    once). Resolves the jid from the order's conversation, invokes the graph with
    `payment_done`, flushes the full-song delivery, and cancels cold follow-ups.
    """
    conv_id = str(order.get("conversation_id") or "")
    jid = await repo.get_jid_by_conversation(conv_id) if conv_id else None
    if not jid:
        logger.warning("deliver: no jid for order %s", order.get("id"))
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


async def on_payment_event(mp_payment_id: str) -> None:
    """Mercado Pago notification (legacy PIX path)."""
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

    await _deliver_for_order(order)


async def on_infinitepay_payment(
    order_nsu: str, transaction_nsu: Optional[str], slug: Optional[str]
) -> None:
    """InfinitePay checkout notification.

    The webhook is UNSIGNED, so it is treated as an untrusted ping: we re-confirm
    authoritatively via /payment_check and require the paid amount to cover the
    price before delivering. A spoofed notification therefore cannot trigger
    delivery.
    """
    order = await repo.get_order_by_mp_payment(order_nsu)  # mp_payment_id == order_nsu
    if not order:
        logger.warning("on_infinitepay_payment: unknown order_nsu %s", order_nsu)
        return
    if (order.get("status") or "").lower() == "paid":
        return  # idempotent: already delivered

    provider = get_payment_provider()
    try:
        res = await provider.payment_check(order_nsu, transaction_nsu, slug)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        logger.exception("payment_check failed for order_nsu %s", order_nsu)
        return

    if not res.get("paid"):
        logger.info("on_infinitepay_payment: not paid yet for %s", order_nsu)
        return
    if int(res.get("amount_cents") or 0) < settings.price_cents:
        logger.warning(
            "on_infinitepay_payment: underpaid %s (%s < %s)",
            order_nsu, res.get("amount_cents"), settings.price_cents,
        )
        return

    paid = await repo.mark_order_paid(order_nsu, txid=transaction_nsu)
    if not paid:
        return  # lost the race / already paid elsewhere

    await _deliver_for_order(order)


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
