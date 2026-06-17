"""Unit tests for app.evolution.parser.parse_upsert (no network).

Runnable two ways:
  * pytest tests/test_evolution_parser.py
  * python3 tests/test_evolution_parser.py   (plain-assert harness at bottom)
"""
from __future__ import annotations

from app.evolution.parser import parse_upsert

USER_JID = "5547999999999@s.whatsapp.net"
GROUP_JID = "120363000000000000@g.us"


def _event(message: dict, *, jid: str = USER_JID, from_me: bool = False) -> dict:
    """Build a minimal messages.upsert event wrapping one message union."""
    return {
        "event": "messages.upsert",
        "instance": "marina",
        "data": {
            "key": {"remoteJid": jid, "fromMe": from_me, "id": "MSGID123"},
            "message": message,
            "pushName": "Rafael",
            "messageTimestamp": 1718200000,
        },
    }


def test_conversation_text():
    msg = parse_upsert(_event({"conversation": "oi marina"}))
    assert msg is not None
    assert msg.kind == "text"
    assert msg.text == "oi marina"
    assert msg.from_me is False
    assert msg.is_group is False
    assert msg.jid == USER_JID
    assert msg.message_id == "MSGID123"
    assert msg.push_name == "Rafael"
    assert msg.timestamp == 1718200000
    assert msg.phone == "5547999999999"
    assert msg.is_actionable is True


def test_extended_text_message():
    msg = parse_upsert(
        _event({"extendedTextMessage": {"text": "quero uma musica"}})
    )
    assert msg is not None
    assert msg.kind == "text"
    assert msg.text == "quero uma musica"
    assert msg.from_me is False
    assert msg.is_group is False
    assert msg.jid == USER_JID


def test_audio_message():
    raw = {"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "ptt": True}}
    msg = parse_upsert(_event(raw))
    assert msg is not None
    assert msg.kind == "audio"
    assert msg.text is None
    assert msg.mimetype == "audio/ogg; codecs=opus"
    # The whole message must survive in .raw so the client can fetch bytes.
    assert msg.raw["message"]["audioMessage"]["ptt"] is True
    assert msg.raw["key"]["id"] == "MSGID123"


def test_image_message_with_caption():
    raw = {"imageMessage": {"caption": "olha essa foto", "mimetype": "image/jpeg"}}
    msg = parse_upsert(_event(raw))
    assert msg is not None
    assert msg.kind == "image"
    assert msg.text == "olha essa foto"
    assert msg.mimetype == "image/jpeg"


def test_group_message_is_group_true():
    msg = parse_upsert(_event({"conversation": "no grupo"}, jid=GROUP_JID))
    assert msg is not None
    assert msg.is_group is True
    assert msg.jid == GROUP_JID
    # Group chats are not actionable for the agent.
    assert msg.is_actionable is False


def test_from_me_message():
    msg = parse_upsert(_event({"conversation": "resposta da marina"}, from_me=True))
    assert msg is not None
    assert msg.from_me is True
    assert msg.is_actionable is False


def test_data_as_list_is_unwrapped():
    event = _event({"conversation": "via lista"})
    event["data"] = [event["data"]]
    msg = parse_upsert(event)
    assert msg is not None
    assert msg.text == "via lista"


def test_non_message_returns_none():
    # No "message" payload (e.g. receipt/protocol update) -> not parseable.
    assert parse_upsert(_event({})) is None
    assert parse_upsert({"data": {"key": {"remoteJid": USER_JID}}}) is None
    assert parse_upsert({}) is None
    assert parse_upsert({"data": {}}) is None


def test_unknown_type_is_other():
    msg = parse_upsert(_event({"stickerMessage": {"mimetype": "image/webp"}}))
    assert msg is not None
    assert msg.kind == "other"
    assert msg.text is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
