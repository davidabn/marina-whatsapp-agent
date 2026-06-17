"""Deterministic-spine tests for the Marina LangGraph state machine.

No network / DB: the LLM brain (`compose`, `extract_slots`, `classify_intent`,
`write_song`) and every external IO (kie, evolution, storage, repo, Mercado
Pago) are monkeypatched, and the graph runs over an in-memory checkpointer.

Covered spine:
1. welcome -> discovery routing.
2. price (R$ 29,90) is NEVER emitted before the anchor stage.
3. anchor consent chains kie.submit and lands in GENERATION_WAIT.
4. pix is only reachable after a preview_url exists (and emits the copia-cola).
5. an OBJECTION_STYLE intent routes back to style/regen and respects
   settings.max_free_regenerations.

Run with `asyncio.run` (no pytest-asyncio dependency).
"""
from __future__ import annotations

import asyncio

import app.db.repo as repo
import app.graph.nodes.pix as pix_node
import app.graph.runner as runner
import app.llm.extract as extract
import app.llm.reply as reply
import app.llm.songwriter as songwriter_llm
import app.music.kie as kie
from app.graph.build import build_graph
from app.llm.extract import Intent
from app.music.schema import KiePayload
from app.payments.base import PixCharge
from langgraph.checkpoint.memory import MemorySaver

_THREAD = 0


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _fake_payload() -> KiePayload:
    return KiePayload(
        title="Para Joao",
        style="sertanejo romantico",
        vocalGender="f",
        prompt="[Chorus - opens song]\nJoao, meu amor\n[Verse 1]\nna chuva voce veio",
    )


def _smart_extract(_history, text, _brief, _stage) -> dict:
    """Drive the discovery flow from the inbound text keywords."""
    low = (text or "").lower()
    if "pode usar" in low:
        return {}
    if any(k in low for k in ("marido", "esposo", "joao", "joão")):
        return {"recipient_name": "Joao", "relationship": "esposo", "singer_gender": "f"}
    if any(k in low for k in ("chuva", "histor", "conhec", "doente")):
        return {"story": text}
    if any(k in low for k in ("sertanejo", "pop", "mpb", "pagode", "gospel", "forro")):
        return {"style_request": text}
    return {}


class _Recorder:
    def __init__(self):
        self.submit_calls = 0
        self.create_order_calls = 0


def _install_fakes(monkeypatch, rec: _Recorder, *, intent=Intent.NORMAL):
    async def fake_compose(history, instruction, *, brief=None, temperature=0.7):
        # Never contains price; the anchor node emits price deterministically.
        return ["resposta da marina 💛"]

    async def fake_classify(text, stage):
        return intent

    async def fake_extract(history, text, brief, stage):
        return _smart_extract(history, text, brief, stage)

    async def fake_write_song(brief, style_resolved, *, call_back_url=None):
        return _fake_payload()

    async def fake_submit(payload):
        rec.submit_calls += 1
        return "task-123"

    async def fake_create_generation(conversation_id, kie_task_id, payload):
        return {"id": "gen-1"}

    async def fake_update_generation(*a, **k):
        return None

    async def fake_create_order(conversation_id, amount_cents, mp_payment_id, pix_copia_cola, txid=None):
        rec.create_order_calls += 1
        return {"id": "order-1"}

    async def fake_schedule_followup(*a, **k):
        return None

    class _FakeMP:
        async def create_pix_charge(self, amount_cents, description, external_ref, payer_email=None):
            return PixCharge(
                payment_id="mp-1", copia_cola="000201PIXCOPIACOLA5204",
                qr_base64=None, status="pending", amount_cents=amount_cents,
            )

    monkeypatch.setattr(reply, "compose", fake_compose)
    monkeypatch.setattr(extract, "classify_intent", fake_classify)
    monkeypatch.setattr(extract, "extract_slots", fake_extract)
    monkeypatch.setattr(songwriter_llm, "write_song", fake_write_song)
    monkeypatch.setattr(kie, "submit", fake_submit)
    monkeypatch.setattr(repo, "create_generation", fake_create_generation)
    monkeypatch.setattr(repo, "update_generation", fake_update_generation)
    monkeypatch.setattr(repo, "create_order", fake_create_order)
    monkeypatch.setattr(repo, "schedule_followup", fake_schedule_followup)
    monkeypatch.setattr(pix_node, "MercadoPagoProvider", _FakeMP)


def _new_graph():
    runner._graph = build_graph(MemorySaver())
    return runner._graph


def _thread() -> str:
    global _THREAD
    _THREAD += 1
    return f"jid-{_THREAD}@s.whatsapp.net"


def _texts(result: dict) -> str:
    return " ".join(i.get("text", "") for i in (result.get("outbound") or []) if i.get("kind") == "text")


async def _seed(graph, jid: str, values: dict):
    await graph.aupdate_state({"configurable": {"thread_id": jid}}, values)


# --------------------------------------------------------------------------- #
# 1. welcome -> discovery
# --------------------------------------------------------------------------- #
def test_welcome_routes_to_discovery(monkeypatch):
    rec = _Recorder()
    _install_fakes(monkeypatch, rec, intent=Intent.GREETING)
    _new_graph()
    jid = _thread()

    res = asyncio.run(runner._invoke(jid, inbound_text="oi", conversation_id="c1"))

    assert res.get("stage") == "discovery_recipient"
    assert res.get("outbound")  # greeting bubbles were emitted


# --------------------------------------------------------------------------- #
# 2. price never emitted before anchor
# --------------------------------------------------------------------------- #
def test_price_not_emitted_before_anchor(monkeypatch):
    rec = _Recorder()
    _install_fakes(monkeypatch, rec, intent=Intent.NORMAL)
    _new_graph()
    jid = _thread()

    async def drive():
        results = []
        for text in (
            "oi",
            "e pro meu marido Joao",
            "ele me buscou na chuva quando eu tava doente",
            "pode usar o que falei",
            "sertanejo",
        ):
            results.append(await runner._invoke(jid, inbound_text=text, conversation_id="c1"))
        return results

    results = asyncio.run(drive())

    # Turns 1-4 (welcome, recipient, story, extras->style) carry no price.
    for res in results[:4]:
        body = _texts(res)
        assert "R$" not in body and "29,90" not in body, body

    # Turn 5 completes STYLE and chains into ANCHOR, where price first appears.
    anchor_body = _texts(results[4])
    assert results[4].get("stage") == "anchor"
    assert "29,90" in anchor_body


# --------------------------------------------------------------------------- #
# 3. anchor consent -> kie.submit + GENERATION_WAIT
# --------------------------------------------------------------------------- #
def test_anchor_consent_submits_and_waits(monkeypatch):
    rec = _Recorder()
    _install_fakes(monkeypatch, rec, intent=Intent.NORMAL)
    graph = _new_graph()
    jid = _thread()

    async def drive():
        await _seed(graph, jid, {
            "stage": "anchor",
            "brief": {
                "recipient_name": "Joao", "relationship": "esposo",
                "singer_gender": "f", "story": "na chuva", "style_request": "sertanejo",
            },
            "extra": {"anchor_explained": True},
            "regen_count": 0,
        })
        return await runner._invoke(jid, inbound_text="pode sim", conversation_id="c1")

    res = asyncio.run(drive())

    assert rec.submit_calls == 1
    assert res.get("stage") == "generation_wait"
    assert res.get("kie_task_id") == "task-123"


# --------------------------------------------------------------------------- #
# 4. pix only reachable after a preview_url exists
# --------------------------------------------------------------------------- #
def test_pix_only_after_preview(monkeypatch):
    rec = _Recorder()
    _install_fakes(monkeypatch, rec, intent=Intent.NORMAL)
    graph = _new_graph()

    # 4a) Without a preview_url, a CHOICE turn must NOT reach pix.
    jid_a = _thread()

    async def no_preview():
        await _seed(graph, jid_a, {
            "stage": "choice",
            "brief": {"recipient_name": "Joao", "relationship": "esposo"},
            "variants": [{"id": "v1", "audio_url": "http://a", "title": "t"}],
            "extra": {},
        })
        return await runner._invoke(jid_a, inbound_text="amei a segunda", conversation_id="c1")

    res_a = asyncio.run(no_preview())
    assert res_a.get("stage") != "pix_wait"
    assert rec.create_order_calls == 0
    assert "PIXCOPIACOLA" not in _texts(res_a)

    # 4b) With a preview_url, the same reaction flows choice -> pix -> PIX_WAIT.
    jid_b = _thread()

    async def with_preview():
        await _seed(graph, jid_b, {
            "stage": "choice",
            "brief": {"recipient_name": "Joao", "relationship": "esposo"},
            "variants": [{"id": "v1", "audio_url": "http://a", "title": "t"}],
            "preview_url": "http://preview/clip.mp3",
            "extra": {},
        })
        return await runner._invoke(jid_b, inbound_text="amei a segunda", conversation_id="c1")

    res_b = asyncio.run(with_preview())
    assert res_b.get("stage") == "pix_wait"
    assert rec.create_order_calls == 1
    assert "PIXCOPIACOLA" in _texts(res_b)


# --------------------------------------------------------------------------- #
# 5. OBJECTION_STYLE -> regen, respecting the cap
# --------------------------------------------------------------------------- #
def test_objection_style_regen_and_cap(monkeypatch):
    rec = _Recorder()
    _install_fakes(monkeypatch, rec, intent=Intent.OBJECTION_STYLE)
    graph = _new_graph()

    base_state = {
        "stage": "choice",
        "brief": {"recipient_name": "Joao", "relationship": "esposo", "style_request": "sertanejo"},
        "variants": [{"id": "v1", "audio_url": "http://a", "title": "t"}],
        "preview_url": "http://preview/clip.mp3",
    }

    # 5a) Under the cap: routes back to STYLE (regen), no resubmit yet.
    jid_a = _thread()

    async def under_cap():
        await _seed(graph, jid_a, {**base_state, "regen_count": 0, "extra": {}})
        return await runner._invoke(jid_a, inbound_text="nao gostei do estilo", conversation_id="c1")

    res_a = asyncio.run(under_cap())
    assert res_a.get("stage") == "style"
    assert (res_a.get("extra") or {}).get("regen") is True
    assert rec.submit_calls == 0  # only re-asked; no regeneration submitted

    # 5b) At the cap (regen_count == max): does NOT regen; escalates instead.
    jid_b = _thread()

    async def at_cap():
        await _seed(graph, jid_b, {
            **base_state,
            "regen_count": runner.settings.max_free_regenerations,
            "extra": {},
        })
        return await runner._invoke(jid_b, inbound_text="nao gostei do estilo", conversation_id="c1")

    res_b = asyncio.run(at_cap())
    assert res_b.get("stage") != "style"
    assert res_b.get("needs_human") is True
    assert rec.submit_calls == 0
