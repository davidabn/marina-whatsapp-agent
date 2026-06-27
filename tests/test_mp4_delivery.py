"""Tests for the paid full-song delivery (Suno MP4 visualizer + fallbacks).

No network / ffmpeg: kie, storage, the local renderer, the contact lock, _flush
and repo are all stubbed. Run with asyncio.run, matching the rest of the suite.
"""
from __future__ import annotations

import asyncio

import app.db.repo as repo
import app.graph.nodes.deliver as deliver_node
import app.graph.runner as runner


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _no_lock(jid):
    return _Lock()


def _wire_common(monkeypatch, capture):
    """Stub storage.upload (returns a URL keyed by content-type), the lock,
    _flush (captures outbound), repo.update_generation, and lyrics."""
    async def fake_upload(path, data, content_type):
        return f"https://supa/{content_type}"

    async def fake_flush(number, result, *, conversation_id=None, contact_id=None):
        capture["outbound"] = result.get("outbound") or []
        capture["number"] = number

    async def fake_update(task, *, full_url=None, completed=False, **k):
        capture["update"] = {"task": task, "full_url": full_url, "completed": completed}

    monkeypatch.setattr(runner.storage, "upload", fake_upload)
    monkeypatch.setattr(runner, "contact_lock", _no_lock)
    monkeypatch.setattr(runner, "_flush", fake_flush)
    monkeypatch.setattr(repo, "update_generation", fake_update)
    monkeypatch.setattr(runner.lyrics, "full_lyrics", lambda p: "a letra" if p else "")


def _brief():
    return {"recipient_name": "Marcio", "relationship": "esposo", "singer_gender": "f"}


# --------------------------------------------------------------------------- #
# Primary: official Suno visualizer
# --------------------------------------------------------------------------- #
def test_visualizer_happy_path(monkeypatch):
    cap = {}
    _wire_common(monkeypatch, cap)

    async def fake_submit(task, audio_id):
        cap["submit"] = (task, audio_id)
        return "mp4task"

    async def fake_poll(mp4_task):
        return "http://temp/video.mp4"

    async def fake_download(url):
        return b"video-bytes"

    monkeypatch.setattr(runner.kie, "submit_mp4", fake_submit)
    monkeypatch.setattr(runner.kie, "poll_mp4", fake_poll)
    monkeypatch.setattr(runner.kie, "download", fake_download)

    chosen = {"id": "aud1", "audio_url": "http://a", "image_url": "http://i"}
    asyncio.run(runner.render_and_send_full_video(
        "5511@s.whatsapp.net", "c1", chosen, "musictask", _brief(), "lyrics-prompt"
    ))

    assert cap["submit"] == ("musictask", "aud1")
    ob = cap["outbound"]
    assert ob[0]["kind"] == "video"
    assert ob[0]["url"] == "https://supa/video/mp4"
    # closing bubbles follow the video, in order
    kinds = [i["kind"] for i in ob]
    assert kinds == ["video", "text", "text", "text"]
    assert "Marcio" in ob[1]["text"]
    assert "letra" in ob[2]["text"]          # lyrics offer (full_lyrics truthy)
    assert cap["update"] == {"task": "musictask", "full_url": "https://supa/video/mp4", "completed": True}


def test_visualizer_failure_falls_back_to_local_video(monkeypatch):
    cap = {}
    _wire_common(monkeypatch, cap)

    async def fake_submit(task, audio_id):
        raise RuntimeError("kie down")

    async def fake_download(url):
        return b"audio" if url == "http://a" else b"image"

    monkeypatch.setattr(runner.kie, "submit_mp4", fake_submit)
    monkeypatch.setattr(runner.kie, "download", fake_download)
    monkeypatch.setattr(runner.preview_mod, "make_full_video", lambda a, i, **k: b"localvid")

    chosen = {"id": "aud1", "audio_url": "http://a", "image_url": "http://i"}
    asyncio.run(runner.render_and_send_full_video(
        "5511@s.whatsapp.net", "c1", chosen, "musictask", _brief(), ""
    ))

    ob = cap["outbound"]
    assert ob[0]["kind"] == "video"
    assert ob[0]["url"] == "https://supa/video/mp4"   # local render, re-hosted
    # no lyrics prompt -> no lyrics-offer bubble
    assert [i["kind"] for i in ob] == ["video", "text", "text"]


def test_no_image_falls_back_to_audio(monkeypatch):
    cap = {}
    _wire_common(monkeypatch, cap)

    async def fake_download(url):
        return b"audio"

    # kie_task_id=None -> skip the visualizer entirely; no image -> skip local video.
    monkeypatch.setattr(runner.kie, "download", fake_download)

    chosen = {"id": "aud1", "audio_url": "http://a", "image_url": ""}
    asyncio.run(runner.render_and_send_full_video(
        "5511@s.whatsapp.net", "c1", chosen, None, _brief(), ""
    ))

    ob = cap["outbound"]
    assert ob[0]["kind"] == "audio"
    assert ob[0]["url"] == "https://supa/audio/mpeg"


def test_total_failure_sends_apology(monkeypatch):
    cap = {}
    _wire_common(monkeypatch, cap)

    async def boom(*a, **k):
        raise RuntimeError("network gone")

    # Visualizer skipped (no task), and the audio download fails too.
    monkeypatch.setattr(runner.kie, "download", boom)

    chosen = {"id": "aud1", "audio_url": "http://a", "image_url": ""}
    asyncio.run(runner.render_and_send_full_video(
        "5511@s.whatsapp.net", "c1", chosen, None, _brief(), ""
    ))

    ob = cap["outbound"]
    assert len(ob) == 1
    assert ob[0]["kind"] == "text"
    assert "engasgada" in ob[0]["text"]
    assert "update" not in cap   # nothing delivered -> no full_url stamped


# --------------------------------------------------------------------------- #
# deliver node: immediate ack + spawns the background render
# --------------------------------------------------------------------------- #
def test_deliver_node_acks_and_spawns(monkeypatch):
    cap = {}

    def fake_spawn(coro, *, name="task"):
        cap["spawn_name"] = name
        coro.close()   # we don't run it here; avoid "never awaited"

    async def fake_schedule(conv_id, kind, run_at):
        cap["followup"] = (conv_id, kind)

    monkeypatch.setattr(deliver_node, "spawn", fake_spawn)
    monkeypatch.setattr(deliver_node.repo, "schedule_followup", fake_schedule)

    state = {
        "brief": _brief(),
        "variants": [{"id": "aud1", "audio_url": "http://a", "image_url": "http://i"}],
        "chosen_variant": "aud1",
        "wa_jid": "5511@s.whatsapp.net",
        "kie_task_id": "musictask",
        "outbound": [],
        "extra": {"conversation_id": "c1", "lyrics_prompt": "p"},
    }
    result = asyncio.run(deliver_node.deliver(state))

    assert result["paid"] is True
    assert result["stage"] == "done"
    texts = [i["text"] for i in result["outbound"] if i["kind"] == "text"]
    assert any("Recebido" in t for t in texts)
    assert cap["spawn_name"].startswith("fullvideo:")
    assert cap["followup"] == ("c1", "postsale")
