"""Logic-only tests for the LLM brain modules.

No network: the ChatOpenAI factory (`get_chat`) is monkeypatched with a fake that
returns canned content, so we exercise bubble-splitting, the tone retry/strip
path, and the deterministic singer-gender inference helper.
"""
from __future__ import annotations

import asyncio

import app.llm.reply as reply
from app.llm.extract import infer_singer_gender
from app.llm.reply import compose, split_bubbles


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """Pops one canned response per `ainvoke` call; records call count."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        return _FakeResponse(self._responses[idx])


def _patch_get_chat(monkeypatch, fake: _FakeLLM):
    monkeypatch.setattr(reply, "get_chat", lambda *a, **k: fake)


# --------------------------------------------------------------------------- #
# split_bubbles
# --------------------------------------------------------------------------- #
def test_split_bubbles_blank_lines():
    text = "Oii!\n\nMe conta uma coisa\n\npra quem e a musica?"
    assert split_bubbles(text) == ["Oii!", "Me conta uma coisa", "pra quem e a musica?"]


def test_split_bubbles_sentinel():
    text = "primeira ||| segunda ||| terceira"
    assert split_bubbles(text) == ["primeira", "segunda", "terceira"]


def test_split_bubbles_single_block_multiline():
    # No blank lines: each non-empty line becomes its own bubble.
    text = "linha um\nlinha dois\nlinha tres"
    assert split_bubbles(text) == ["linha um", "linha dois", "linha tres"]


def test_split_bubbles_single_line_and_empty():
    assert split_bubbles("uma frase so") == ["uma frase so"]
    assert split_bubbles("") == []
    assert split_bubbles("   ") == []


def test_split_bubbles_strips_bullets():
    assert split_bubbles("- oi\n\n* tudo bem") == ["oi", "tudo bem"]


# --------------------------------------------------------------------------- #
# infer_singer_gender
# --------------------------------------------------------------------------- #
def test_infer_singer_gender_buyer_is_female():
    assert infer_singer_gender("e pro meu marido Joao") == "f"
    assert infer_singer_gender("meu namorado faz aniversario") == "f"


def test_infer_singer_gender_buyer_is_male():
    assert infer_singer_gender("e pra minha esposa Maria") == "m"
    assert infer_singer_gender("quero pra minha namorada") == "m"


def test_infer_singer_gender_ambiguous_returns_none():
    assert infer_singer_gender("e pra minha mae") is None
    assert infer_singer_gender("uma musica pro meu pai") is None
    assert infer_singer_gender("") is None


# --------------------------------------------------------------------------- #
# compose tone post-filter
# --------------------------------------------------------------------------- #
def test_compose_clean_first_try(monkeypatch):
    fake = _FakeLLM(["Oii!\n\nque amor 💛"])
    _patch_get_chat(monkeypatch, fake)

    bubbles = asyncio.run(compose([], "saudar o cliente"))

    assert bubbles == ["Oii!", "que amor 💛"]
    assert fake.calls == 1  # no retry needed


def test_compose_retries_once_on_violation(monkeypatch):
    # First attempt has a banned emoji; retry comes back clean.
    fake = _FakeLLM(["vamos fechar 🚀", "que linda historia 💛"])
    _patch_get_chat(monkeypatch, fake)

    bubbles = asyncio.run(compose([], "responder"))

    assert fake.calls == 2  # retried exactly once
    assert bubbles == ["que linda historia 💛"]
    assert all("🚀" not in b for b in bubbles)


def test_compose_strips_emoji_when_retry_still_violates(monkeypatch):
    # Both attempts keep the banned emoji -> last resort strips it.
    fake = _FakeLLM(["compra agora 🔥", "ainda 🔥 aqui"])
    _patch_get_chat(monkeypatch, fake)

    bubbles = asyncio.run(compose([], "responder"))

    assert fake.calls == 2
    assert bubbles  # not empty
    assert all("🔥" not in b for b in bubbles)
    assert bubbles == ["ainda aqui"]
