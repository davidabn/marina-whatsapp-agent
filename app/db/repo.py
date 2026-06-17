"""Async data-access layer for the business tables (db/migrations/0001_init.sql).

Single psycopg async pool, shared with the app lifespan. All functions take a
connection from the pool. This is the ONLY module that writes SQL for business
tables — nodes/webhooks call these helpers, never raw SQL.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

_pool: Optional[AsyncConnectionPool] = None


async def init_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=settings.supabase_db_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=False,
        )
        await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized; call init_pool() at startup.")
    return _pool


# --- contacts -------------------------------------------------------------
async def get_or_create_contact(
    wa_jid: str,
    push_name: Optional[str] = None,
    phone: Optional[str] = None,
    source_ad: Optional[str] = None,
) -> dict[str, Any]:
    async with pool().connection() as conn:
        row = await (await conn.execute(
            """
            insert into contacts (wa_jid, push_name, phone, source_ad)
            values (%s, %s, %s, %s)
            on conflict (wa_jid) do update
                set last_seen_at = now(),
                    push_name = coalesce(excluded.push_name, contacts.push_name)
            returning *
            """,
            (wa_jid, push_name, phone, source_ad),
        )).fetchone()
        return row


async def set_needs_human(contact_id: str, value: bool = True) -> None:
    async with pool().connection() as conn:
        await conn.execute(
            "update contacts set needs_human = %s where id = %s", (value, contact_id)
        )


# --- conversations --------------------------------------------------------
async def get_or_create_conversation(contact_id: str) -> dict[str, Any]:
    async with pool().connection() as conn:
        existing = await (await conn.execute(
            "select * from conversations where contact_id = %s order by created_at desc limit 1",
            (contact_id,),
        )).fetchone()
        if existing:
            return existing
        return await (await conn.execute(
            "insert into conversations (contact_id) values (%s) returning *",
            (contact_id,),
        )).fetchone()


async def get_conversation_by_jid(wa_jid: str) -> Optional[dict[str, Any]]:
    async with pool().connection() as conn:
        return await (await conn.execute(
            """
            select c.* from conversations c
            join contacts ct on ct.id = c.contact_id
            where ct.wa_jid = %s
            order by c.created_at desc limit 1
            """,
            (wa_jid,),
        )).fetchone()


async def get_jid_by_conversation(conversation_id: str) -> Optional[str]:
    """Resolve the WhatsApp JID for a conversation (used by webhook/scheduler
    resume paths, which only have a conversation_id)."""
    async with pool().connection() as conn:
        row = await (await conn.execute(
            """
            select ct.wa_jid from contacts ct
            join conversations c on c.contact_id = ct.id
            where c.id = %s
            """,
            (conversation_id,),
        )).fetchone()
        return row["wa_jid"] if row else None


async def update_conversation(
    conversation_id: str,
    *,
    stage: Optional[str] = None,
    brief: Optional[dict] = None,
    chosen_variant: Optional[str] = None,
    regen_count: Optional[int] = None,
) -> None:
    sets, params = [], []
    if stage is not None:
        sets.append("stage = %s"); params.append(stage)
    if brief is not None:
        sets.append("brief = %s"); params.append(json.dumps(brief))
    if chosen_variant is not None:
        sets.append("chosen_variant = %s"); params.append(chosen_variant)
    if regen_count is not None:
        sets.append("regen_count = %s"); params.append(regen_count)
    if not sets:
        return
    sets.append("updated_at = now()")
    params.append(conversation_id)
    async with pool().connection() as conn:
        await conn.execute(
            f"update conversations set {', '.join(sets)} where id = %s", params
        )


# --- messages -------------------------------------------------------------
async def message_exists(wa_message_id: str) -> bool:
    async with pool().connection() as conn:
        row = await (await conn.execute(
            "select 1 from messages where wa_message_id = %s", (wa_message_id,)
        )).fetchone()
        return row is not None


async def log_message(
    *,
    conversation_id: Optional[str],
    contact_id: Optional[str],
    direction: str,
    kind: str = "text",
    content: Optional[str] = None,
    wa_message_id: Optional[str] = None,
    media_url: Optional[str] = None,
    transcript: Optional[str] = None,
    raw: Optional[dict] = None,
) -> None:
    async with pool().connection() as conn:
        await conn.execute(
            """
            insert into messages
                (conversation_id, contact_id, direction, kind, content,
                 wa_message_id, media_url, transcript, raw)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (wa_message_id) do nothing
            """,
            (conversation_id, contact_id, direction, kind, content,
             wa_message_id, media_url, transcript,
             json.dumps(raw) if raw is not None else None),
        )


# --- generations ----------------------------------------------------------
async def create_generation(conversation_id: str, kie_task_id: str, payload: dict) -> dict:
    async with pool().connection() as conn:
        return await (await conn.execute(
            """
            insert into generations (conversation_id, kie_task_id, payload, status)
            values (%s,%s,%s,'PENDING')
            on conflict (kie_task_id) do update set payload = excluded.payload
            returning *
            """,
            (conversation_id, kie_task_id, json.dumps(payload)),
        )).fetchone()


async def get_generation_by_task(kie_task_id: str) -> Optional[dict]:
    async with pool().connection() as conn:
        return await (await conn.execute(
            "select * from generations where kie_task_id = %s", (kie_task_id,)
        )).fetchone()


async def update_generation(
    kie_task_id: str,
    *,
    status: Optional[str] = None,
    variants: Optional[list] = None,
    preview_url: Optional[str] = None,
    full_url: Optional[str] = None,
    error: Optional[str] = None,
    completed: bool = False,
) -> None:
    sets, params = [], []
    if status is not None:
        sets.append("status = %s"); params.append(status)
    if variants is not None:
        sets.append("variants = %s"); params.append(json.dumps(variants))
    if preview_url is not None:
        sets.append("preview_url = %s"); params.append(preview_url)
    if full_url is not None:
        sets.append("full_url = %s"); params.append(full_url)
    if error is not None:
        sets.append("error = %s"); params.append(error)
    if completed:
        sets.append("completed_at = now()")
    if not sets:
        return
    params.append(kie_task_id)
    async with pool().connection() as conn:
        await conn.execute(
            f"update generations set {', '.join(sets)} where kie_task_id = %s", params
        )


# --- orders ---------------------------------------------------------------
async def create_order(conversation_id: str, amount_cents: int,
                       mp_payment_id: str, pix_copia_cola: str,
                       txid: Optional[str] = None) -> dict:
    async with pool().connection() as conn:
        return await (await conn.execute(
            """
            insert into orders (conversation_id, amount_cents, mp_payment_id, pix_copia_cola, txid)
            values (%s,%s,%s,%s,%s)
            on conflict (mp_payment_id) do update set pix_copia_cola = excluded.pix_copia_cola
            returning *
            """,
            (conversation_id, amount_cents, mp_payment_id, pix_copia_cola, txid),
        )).fetchone()


async def get_order_by_mp_payment(mp_payment_id: str) -> Optional[dict]:
    async with pool().connection() as conn:
        return await (await conn.execute(
            "select * from orders where mp_payment_id = %s", (mp_payment_id,)
        )).fetchone()


async def mark_order_paid(mp_payment_id: str) -> Optional[dict]:
    """Idempotent: only flips pending->paid once; returns the updated row."""
    async with pool().connection() as conn:
        return await (await conn.execute(
            """
            update orders set status = 'paid', paid_at = now()
            where mp_payment_id = %s and status <> 'paid'
            returning *
            """,
            (mp_payment_id,),
        )).fetchone()


# --- followups ------------------------------------------------------------
async def schedule_followup(conversation_id: str, kind: str, run_at) -> None:
    async with pool().connection() as conn:
        await conn.execute(
            "insert into followups (conversation_id, kind, run_at) values (%s,%s,%s)",
            (conversation_id, kind, run_at),
        )


async def due_followups(now) -> list[dict]:
    async with pool().connection() as conn:
        return await (await conn.execute(
            "select * from followups where status = 'scheduled' and run_at <= %s order by run_at",
            (now,),
        )).fetchall()


async def mark_followup_sent(followup_id: str) -> None:
    async with pool().connection() as conn:
        await conn.execute(
            "update followups set status = 'sent', sent_at = now() where id = %s",
            (followup_id,),
        )


async def cancel_pending_followups(conversation_id: str) -> None:
    async with pool().connection() as conn:
        await conn.execute(
            "update followups set status = 'cancelled' "
            "where conversation_id = %s and status = 'scheduled'",
            (conversation_id,),
        )
