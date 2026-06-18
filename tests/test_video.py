"""Tests for the 45s video preview path."""
from app.graph.nodes import emit_video
from app.music.schema import Variant


def test_emit_video_appends_item():
    state = {"outbound": []}
    emit_video(state, "https://x/v.mp4", caption="oi")
    assert state["outbound"] == [{"kind": "video", "url": "https://x/v.mp4", "caption": "oi"}]


def test_emit_video_empty_url_is_noop():
    state = {"outbound": []}
    assert emit_video(state, "") == []
    assert state["outbound"] == []


def test_variant_carries_image_url():
    v = Variant(id="a", audio_url="http://a/x.mp3", title="t", image_url="http://a/c.jpg")
    assert v.image_url == "http://a/c.jpg"
    # image_url is optional (defaults to "")
    assert Variant(id="b", audio_url="http://b/y.mp3").image_url == ""
