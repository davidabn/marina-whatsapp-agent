"""Async data-access layer for the business tables (db/migrations/0001_init.sql).

Talks to self-hosted Supabase via its PostgREST HTTP gateway (kong) — the
internal Postgres is network-unreachable from the app, but the REST endpoint is.
Every call is stateless: it opens a fresh httpx client carrying the service-role
key (same style as app/music/kie.py). This is the ONLY module that touches the
business tables — nodes/webhooks call these helpers, never PostgREST directly.

NOTE: the LangGraph checkpointer is unrelated to this module — it keeps using a
direct Postgres connection (settings.supabase_db_url) in app/graph/runner.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.config import settings

_TIMEOUT = 30.0


# --- low-level PostgREST plumbing ----------------------------------------
def _base() -> str:
    return f"{settings.supabase_url}/rest/v1"


def _headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    h = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _check(resp: httpx.Response) -> None:
    """PostgREST returns 2xx on success; raise a clear RuntimeError otherwise."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"PostgREST error {resp.status_code} on "
            f"{resp.request.method} {resp.request.url}: {resp.text}"
        ) from exc


def _body(resp: httpx.Response) -> Any:
    """Parsed JSON, or None for empty bodies (204 / return=minimal)."""
    return resp.json() if resp.content else None


async def _get(path: str, params: dict[str, str]) -> list[dict]:
    async with httpx.AsyncClient(base_url=_base(), timeout=_TIMEOUT) as client:
        resp = await client.get(path, params=params, headers=_headers())
    _check(resp)
    return resp.json()


async def _post(
    path: str,
    body: Any,
    *,
    params: Optional[dict[str, str]] = None,
    prefer: Optional[str] = None,
) -> Any:
    headers = _headers({"Prefer": prefer} if prefer else None)
    async with httpx.AsyncClient(base_url=_base(), timeout=_TIMEOUT) as client:
        resp = await client.post(path, params=params, json=body, headers=headers)
    _check(resp)
    return _body(resp)


async def _patch(
    path: str,
    params: dict[str, str],
    body: Any,
    *,
    prefer: Optional[str] = None,
) -> Any:
    headers = _headers({"Prefer": prefer} if prefer else None)
    async with httpx.AsyncClient(base_url=_base(), timeout=_TIMEOUT) as client:
        resp = await client.patch(path, params=params, json=body, headers=headers)
    _check(resp)
    return _body(resp)


async def _delete(
    path: str,
    params: dict[str, str],
    *,
    prefer: Optional[str] = None,
) -> Any:
    headers = _headers({"Prefer": prefer} if prefer else None)
    async with httpx.AsyncClient(base_url=_base(), timeout=_TIMEOUT) as client:
        resp = await client.request("DELETE", path, params=params, headers=headers)
    _check(resp)
    return _body(resp)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


async def healthcheck() -> bool:
    """Ping PostgREST for /health. True if the gateway answers below 500."""
    try:
        async with httpx.AsyncClient(base_url=_base(), timeout=_TIMEOUT) as client:
            resp = await client.get("/", headers=_headers())
        return resp.status_code < 500
    except Exception:  # noqa: BLE001
        return False


# --- contacts -------------------------------------------------------------
async def get_or_create_contact(
    wa_jid: str,
    push_name: Optional[str] = None,
    phone: Optional[str] = None,
    source_ad: Optional[str] = None,
) -> dict[str, Any]:
    rows = await _get("/contacts", {"wa_jid": f"eq.{wa_jid}", "limit": "1"})
    if rows:
        existing = rows[0]
        # Mirror `on conflict do update set last_seen_at = now(),
        # push_name = coalesce(excluded.push_name, contacts.push_name)`:
        # only overwrite push_name when a new (non-null) one is supplied.
        changes: dict[str, Any] = {"last_seen_at": _now_iso()}
        if push_name is not None:
            changes["push_name"] = push_name
        updated = await _patch(
            "/contacts", {"wa_jid": f"eq.{wa_jid}"}, changes,
            prefer="return=representation",
        )
        return updated[0] if updated else existing
    created = await _post(
        "/contacts",
        {"wa_jid": wa_jid, "push_name": push_name, "phone": phone, "source_ad": source_ad},
        prefer="return=representation",
    )
    return created[0]


async def set_needs_human(contact_id: str, value: bool = True) -> None:
    await _patch("/contacts", {"id": f"eq.{contact_id}"}, {"needs_human": value})


async def find_contacts(needle: str) -> list[dict]:
    """Contacts whose wa_jid or phone contains `needle` (digits or full jid).

    Used by the reset tool so the caller can pass just a phone number and we
    recover the exact stored wa_jid (= the LangGraph thread_id).
    """
    return await _get(
        "/contacts",
        {"or": f"(wa_jid.like.*{needle}*,phone.like.*{needle}*)"},
    )


async def delete_contact(wa_jid: str) -> int:
    """Delete a contact by exact wa_jid; cascades to all child rows.

    Returns how many contact rows were removed.
    """
    rows = await _delete(
        "/contacts", {"wa_jid": f"eq.{wa_jid}"}, prefer="return=representation"
    )
    return len(rows or [])


# --- conversations --------------------------------------------------------
async def get_or_create_conversation(contact_id: str) -> dict[str, Any]:
    rows = await _get(
        "/conversations",
        {"contact_id": f"eq.{contact_id}", "order": "created_at.desc", "limit": "1"},
    )
    if rows:
        return rows[0]
    created = await _post(
        "/conversations", {"contact_id": contact_id}, prefer="return=representation"
    )
    return created[0]


async def get_conversation_by_jid(wa_jid: str) -> Optional[dict[str, Any]]:
    contacts = await _get(
        "/contacts", {"wa_jid": f"eq.{wa_jid}", "select": "id", "limit": "1"}
    )
    if not contacts:
        return None
    rows = await _get(
        "/conversations",
        {"contact_id": f"eq.{contacts[0]['id']}", "order": "created_at.desc", "limit": "1"},
    )
    return rows[0] if rows else None


async def get_jid_by_conversation(conversation_id: str) -> Optional[str]:
    """Resolve the WhatsApp JID for a conversation (used by webhook/scheduler
    resume paths, which only have a conversation_id)."""
    rows = await _get(
        "/conversations",
        {
            "id": f"eq.{conversation_id}",
            "select": "contact_id,contacts(wa_jid)",
            "limit": "1",
        },
    )
    if not rows:
        return None
    contact = rows[0].get("contacts")
    if isinstance(contact, list):
        contact = contact[0] if contact else None
    if not contact:
        return None
    return contact.get("wa_jid")


async def update_conversation(
    conversation_id: str,
    *,
    stage: Optional[str] = None,
    brief: Optional[dict] = None,
    chosen_variant: Optional[str] = None,
    regen_count: Optional[int] = None,
) -> None:
    changes: dict[str, Any] = {}
    if stage is not None:
        changes["stage"] = stage
    if brief is not None:
        changes["brief"] = brief
    if chosen_variant is not None:
        changes["chosen_variant"] = chosen_variant
    if regen_count is not None:
        changes["regen_count"] = regen_count
    if not changes:
        return
    changes["updated_at"] = _now_iso()
    await _patch("/conversations", {"id": f"eq.{conversation_id}"}, changes)


# --- messages -------------------------------------------------------------
async def message_exists(wa_message_id: str) -> bool:
    rows = await _get(
        "/messages", {"wa_message_id": f"eq.{wa_message_id}", "select": "id", "limit": "1"}
    )
    return bool(rows)


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
    # Mirror `on conflict (wa_message_id) do nothing` via an ignore-duplicates
    # upsert keyed on wa_message_id (NULL ids never conflict, so they insert).
    await _post(
        "/messages",
        {
            "conversation_id": conversation_id,
            "contact_id": contact_id,
            "direction": direction,
            "kind": kind,
            "content": content,
            "wa_message_id": wa_message_id,
            "media_url": media_url,
            "transcript": transcript,
            "raw": raw,
        },
        params={"on_conflict": "wa_message_id"},
        prefer="resolution=ignore-duplicates,return=minimal",
    )


# --- generations ----------------------------------------------------------
async def create_generation(conversation_id: str, kie_task_id: str, payload: dict) -> dict:
    # `on conflict (kie_task_id) do update set payload = excluded.payload`:
    # only the payload changes on conflict, so GET-then-PATCH (not a full upsert
    # which would also reset status/conversation_id).
    existing = await _get(
        "/generations", {"kie_task_id": f"eq.{kie_task_id}", "limit": "1"}
    )
    if existing:
        updated = await _patch(
            "/generations", {"kie_task_id": f"eq.{kie_task_id}"}, {"payload": payload},
            prefer="return=representation",
        )
        return updated[0] if updated else existing[0]
    created = await _post(
        "/generations",
        {
            "conversation_id": conversation_id,
            "kie_task_id": kie_task_id,
            "payload": payload,
            "status": "PENDING",
        },
        prefer="return=representation",
    )
    return created[0]


async def get_generation_by_task(kie_task_id: str) -> Optional[dict]:
    rows = await _get(
        "/generations", {"kie_task_id": f"eq.{kie_task_id}", "limit": "1"}
    )
    return rows[0] if rows else None


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
    changes: dict[str, Any] = {}
    if status is not None:
        changes["status"] = status
    if variants is not None:
        changes["variants"] = variants
    if preview_url is not None:
        changes["preview_url"] = preview_url
    if full_url is not None:
        changes["full_url"] = full_url
    if error is not None:
        changes["error"] = error
    if completed:
        changes["completed_at"] = _now_iso()
    if not changes:
        return
    await _patch("/generations", {"kie_task_id": f"eq.{kie_task_id}"}, changes)


# --- orders ---------------------------------------------------------------
async def create_order(conversation_id: str, amount_cents: int,
                       mp_payment_id: str, pix_copia_cola: str,
                       txid: Optional[str] = None) -> dict:
    # `on conflict (mp_payment_id) do update set pix_copia_cola = excluded...`:
    # only pix_copia_cola changes on conflict — GET-then-PATCH.
    existing = await _get(
        "/orders", {"mp_payment_id": f"eq.{mp_payment_id}", "limit": "1"}
    )
    if existing:
        updated = await _patch(
            "/orders", {"mp_payment_id": f"eq.{mp_payment_id}"},
            {"pix_copia_cola": pix_copia_cola},
            prefer="return=representation",
        )
        return updated[0] if updated else existing[0]
    created = await _post(
        "/orders",
        {
            "conversation_id": conversation_id,
            "amount_cents": amount_cents,
            "mp_payment_id": mp_payment_id,
            "pix_copia_cola": pix_copia_cola,
            "txid": txid,
        },
        prefer="return=representation",
    )
    return created[0]


async def get_order_by_mp_payment(mp_payment_id: str) -> Optional[dict]:
    rows = await _get(
        "/orders", {"mp_payment_id": f"eq.{mp_payment_id}", "limit": "1"}
    )
    return rows[0] if rows else None


async def mark_order_paid(mp_payment_id: str) -> Optional[dict]:
    """Idempotent flip pending->paid. Returns the row ONLY if THIS call flipped
    it; None if it was already paid (conditional update matched no row). The
    caller delivers exactly once on this — a duplicate Mercado Pago webhook gets
    None and stops, preventing double delivery."""
    updated = await _patch(
        "/orders",
        {"mp_payment_id": f"eq.{mp_payment_id}", "status": "neq.paid"},
        {"status": "paid", "paid_at": _now_iso()},
        prefer="return=representation",
    )
    return updated[0] if updated else None


# --- followups ------------------------------------------------------------
async def schedule_followup(conversation_id: str, kind: str, run_at) -> None:
    await _post(
        "/followups",
        {"conversation_id": conversation_id, "kind": kind, "run_at": _iso(run_at)},
        prefer="return=minimal",
    )


async def due_followups(now) -> list[dict]:
    return await _get(
        "/followups",
        {
            "status": "eq.scheduled",
            "run_at": f"lte.{_iso(now)}",
            "order": "run_at",
            "select": "*",
        },
    )


async def mark_followup_sent(followup_id: str) -> None:
    await _patch(
        "/followups", {"id": f"eq.{followup_id}"},
        {"status": "sent", "sent_at": _now_iso()},
    )


async def cancel_pending_followups(conversation_id: str) -> None:
    await _patch(
        "/followups",
        {"conversation_id": f"eq.{conversation_id}", "status": "eq.scheduled"},
        {"status": "cancelled"},
    )
