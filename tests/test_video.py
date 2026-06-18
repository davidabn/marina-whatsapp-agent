"""Tests for the view-once music-video preview path (Commit 4)."""
import asyncio

from app.graph.nodes import emit_video
from app.graph.router import router
from app.music import kie


def test_emit_video_view_once():
    state = {"outbound": []}
    emit_video(state, "https://x/v.mp4", caption="oi", view_once=True)
    assert state["outbound"] == [
        {"kind": "video", "url": "https://x/v.mp4", "caption": "oi", "view_once": True}
    ]


def test_emit_video_empty_url_is_noop():
    state = {"outbound": []}
    assert emit_video(state, "") == []
    assert state["outbound"] == []


def test_extract_video_url_shapes():
    assert kie.extract_video_url({"video_url": "a"}) == "a"
    assert kie.extract_video_url({"videoUrl": "b"}) == "b"
    assert kie.extract_video_url({"response": {"videoUrl": "c"}}) == "c"
    assert kie.extract_video_url({}) == ""


def test_router_routes_video_done_to_video_ready():
    state = {"extra": {"event": "video_done"}, "stage": "generation_wait", "outbound": []}
    out = asyncio.run(router(state))
    assert out["extra"]["_next"] == "video_ready"
    assert "event" not in out["extra"]  # one-shot event cleared


def test_router_video_timeout_forces_audio_preview():
    state = {"extra": {"event": "video_timeout"}, "stage": "generation_wait", "outbound": []}
    out = asyncio.run(router(state))
    assert out["extra"]["_next"] == "preview"
    assert out["extra"]["force_audio"] is True
