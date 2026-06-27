"""Tests for the KIE safety-net poller + on_generation_complete idempotency.

No network: repo / kie / the contact lock / _invoke / _flush are all stubbed.
Run with asyncio.run, matching the rest of the suite.
"""
from __future__ import annotations

import asyncio

import app.db.repo as repo
import app.graph.runner as runner
from app.music.schema import GenerationResult, Variant


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _no_lock(jid):
    return _Lock()


def _ok_result():
    return GenerationResult(
        task_id="t1", status="SUCCESS",
        variants=[Variant(id="v1", audio_url="http://a", title="x", image_url="http://i")],
    )


# --------------------------------------------------------------------------- #
# poll_generation
# --------------------------------------------------------------------------- #
def test_poll_generation_drives_preview_on_success(monkeypatch):
    rec = {"complete": 0, "polled": 0}

    async def fake_get_gen(task):
        return {"kie_task_id": task, "preview_url": None}

    async def fake_poll(task):
        rec["polled"] += 1
        return _ok_result()

    async def fake_complete(task):
        rec["complete"] += 1

    monkeypatch.setattr(repo, "get_generation_by_task", fake_get_gen)
    monkeypatch.setattr(runner.kie, "poll", fake_poll)
    monkeypatch.setattr(runner, "on_generation_complete", fake_complete)

    asyncio.run(runner.poll_generation("t1"))
    assert rec["polled"] == 1
    assert rec["complete"] == 1


def test_poll_generation_noop_when_already_delivered(monkeypatch):
    rec = {"complete": 0, "polled": 0}

    async def fake_get_gen(task):
        return {"kie_task_id": task, "preview_url": "https://x/preview.mp4"}

    async def fake_poll(task):
        rec["polled"] += 1
        return _ok_result()

    async def fake_complete(task):
        rec["complete"] += 1

    monkeypatch.setattr(repo, "get_generation_by_task", fake_get_gen)
    monkeypatch.setattr(runner.kie, "poll", fake_poll)
    monkeypatch.setattr(runner, "on_generation_complete", fake_complete)

    asyncio.run(runner.poll_generation("t1"))
    assert rec["polled"] == 0     # short-circuits before polling
    assert rec["complete"] == 0


def test_poll_generation_noop_on_poll_failure(monkeypatch):
    rec = {"complete": 0}

    async def fake_get_gen(task):
        return {"kie_task_id": task, "preview_url": None}

    async def fake_poll(task):
        return GenerationResult(task_id=task, status="TIMEOUT", variants=[], error="exhausted")

    async def fake_complete(task):
        rec["complete"] += 1

    monkeypatch.setattr(repo, "get_generation_by_task", fake_get_gen)
    monkeypatch.setattr(runner.kie, "poll", fake_poll)
    monkeypatch.setattr(runner, "on_generation_complete", fake_complete)

    asyncio.run(runner.poll_generation("t1"))
    assert rec["complete"] == 0


# --------------------------------------------------------------------------- #
# on_generation_complete idempotency (webhook x poller dedupe)
# --------------------------------------------------------------------------- #
def test_complete_skips_when_preview_already_set(monkeypatch):
    rec = {"fetch": 0, "invoke": 0}

    async def fake_get_gen(task):
        return {"conversation_id": "c1", "preview_url": "https://x/p.mp4"}

    async def fake_fetch(task):
        rec["fetch"] += 1
        return _ok_result()

    monkeypatch.setattr(repo, "get_generation_by_task", fake_get_gen)
    monkeypatch.setattr(runner.kie, "fetch_result", fake_fetch)
    monkeypatch.setattr(runner, "_invoke", lambda *a, **k: rec.__setitem__("invoke", rec["invoke"] + 1))

    asyncio.run(runner.on_generation_complete("t1"))
    assert rec["fetch"] == 0     # bailed at the top, before re-confirming
    assert rec["invoke"] == 0


def test_complete_rechecks_preview_inside_lock(monkeypatch):
    # Top check passes (no preview), but by the time we hold the lock the OTHER
    # caller has delivered (preview_url now set) -> we must NOT invoke again.
    seq = iter([
        {"conversation_id": "c1", "preview_url": None},   # top check
        {"conversation_id": "c1", "preview_url": "https://x/p.mp4"},  # inside lock
    ])
    rec = {"invoke": 0, "flush": 0}

    async def fake_get_gen(task):
        return next(seq)

    async def fake_jid(cid):
        return "5511@s.whatsapp.net"

    async def fake_fetch(task):
        return _ok_result()

    async def fake_update(*a, **k):
        return None

    async def fake_invoke(*a, **k):
        rec["invoke"] += 1
        return {}

    async def fake_flush(*a, **k):
        rec["flush"] += 1

    monkeypatch.setattr(repo, "get_generation_by_task", fake_get_gen)
    monkeypatch.setattr(repo, "get_jid_by_conversation", fake_jid)
    monkeypatch.setattr(repo, "update_generation", fake_update)
    monkeypatch.setattr(runner.kie, "fetch_result", fake_fetch)
    monkeypatch.setattr(runner, "contact_lock", _no_lock)
    monkeypatch.setattr(runner, "_invoke", fake_invoke)
    monkeypatch.setattr(runner, "_flush", fake_flush)

    asyncio.run(runner.on_generation_complete("t1"))
    assert rec["invoke"] == 0
    assert rec["flush"] == 0


def test_complete_happy_path_invokes_once(monkeypatch):
    rec = {"invoke": 0, "flush": 0}

    async def fake_get_gen(task):
        return {"conversation_id": "c1", "preview_url": None}   # both checks: no preview

    async def fake_jid(cid):
        return "5511@s.whatsapp.net"

    async def fake_fetch(task):
        return _ok_result()

    async def fake_update(*a, **k):
        return None

    async def fake_invoke(*a, **k):
        rec["invoke"] += 1
        return {"outbound": []}

    async def fake_flush(*a, **k):
        rec["flush"] += 1

    monkeypatch.setattr(repo, "get_generation_by_task", fake_get_gen)
    monkeypatch.setattr(repo, "get_jid_by_conversation", fake_jid)
    monkeypatch.setattr(repo, "update_generation", fake_update)
    monkeypatch.setattr(runner.kie, "fetch_result", fake_fetch)
    monkeypatch.setattr(runner, "contact_lock", _no_lock)
    monkeypatch.setattr(runner, "_invoke", fake_invoke)
    monkeypatch.setattr(runner, "_flush", fake_flush)

    asyncio.run(runner.on_generation_complete("t1"))
    assert rec["invoke"] == 1
    assert rec["flush"] == 1
