"""Tests for the InfinitePay checkout integration.

No network: the httpx client is faked for the provider tests, and the runner's
delivery tail is stubbed for the confirmation tests. Run with asyncio.run (no
pytest-asyncio dependency), matching the rest of the suite.
"""
from __future__ import annotations

import asyncio

import app.db.repo as repo
import app.graph.runner as runner
from app.payments.infinitepay import InfinityPayProvider


# --------------------------------------------------------------------------- #
# Fake httpx client
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data):
        self._data = data
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append((url, json))
        return _FakeResp(self._data)


# --------------------------------------------------------------------------- #
# Provider: create checkout link
# --------------------------------------------------------------------------- #
def test_create_checkout_builds_link_and_body():
    client = _FakeClient({"url": "https://checkout.infinitepay.com.br/marina?lenc=abc"})
    prov = InfinityPayProvider(
        handle="marina", webhook_url="https://host/webhooks/infinitepay", client=client
    )
    charge = asyncio.run(
        prov.create_pix_charge(
            2990, "Musica personalizada para Vanessa", "ord123",
            customer={"name": "Rafael", "phone_number": "5547999", "junk": "x"},
        )
    )

    assert charge.checkout_url.startswith("https://checkout.infinitepay.com.br/")
    assert charge.copia_cola == charge.checkout_url   # nothing to "paste"
    assert charge.payment_id == "ord123"              # order_nsu round-trips
    assert charge.amount_cents == 2990

    url, body = client.calls[0]
    assert url.endswith("/links")
    assert body["handle"] == "marina"
    assert body["order_nsu"] == "ord123"
    assert body["items"] == [
        {"quantity": 1, "price": 2990, "description": "Musica personalizada para Vanessa"}
    ]
    assert body["webhook_url"].endswith("/webhooks/infinitepay")
    # only known customer keys are forwarded
    assert body["customer"] == {"name": "Rafael", "phone_number": "5547999"}


def test_create_checkout_requires_handle():
    prov = InfinityPayProvider(handle="", client=_FakeClient({"url": "x"}))
    try:
        asyncio.run(prov.create_pix_charge(2990, "d", "ref"))
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError when handle missing")


# --------------------------------------------------------------------------- #
# Provider: payment_check
# --------------------------------------------------------------------------- #
def test_payment_check_parses_paid():
    client = _FakeClient({"success": True, "paid": True, "paid_amount": 2990})
    prov = InfinityPayProvider(handle="marina", client=client)
    res = asyncio.run(prov.payment_check("ord123", "tx1", "slug1"))

    assert res["paid"] is True
    assert res["amount_cents"] == 2990
    url, body = client.calls[0]
    assert url.endswith("/payment_check")
    assert body == {
        "handle": "marina", "order_nsu": "ord123",
        "transaction_nsu": "tx1", "slug": "slug1",
    }


def test_payment_check_unpaid():
    client = _FakeClient({"success": True, "paid": False})
    prov = InfinityPayProvider(handle="marina", client=client)
    res = asyncio.run(prov.payment_check("ord123", "tx1", "slug1"))
    assert res["paid"] is False
    assert res["amount_cents"] == 0


# --------------------------------------------------------------------------- #
# Runner: on_infinitepay_payment confirmation + anti-fraud
# --------------------------------------------------------------------------- #
def _wire_runner(monkeypatch, *, order, check_result, mark_returns):
    """Stub repo + provider + delivery; return recorders."""
    rec = {"delivered": [], "mark_calls": [], "check_calls": 0}

    async def fake_get_order(nsu):
        return order

    async def fake_mark_paid(nsu, txid=None):
        rec["mark_calls"].append((nsu, txid))
        return mark_returns

    async def fake_deliver(o):
        rec["delivered"].append(o)

    class _P:
        async def payment_check(self, order_nsu, transaction_nsu, slug):
            rec["check_calls"] += 1
            return check_result

    monkeypatch.setattr(repo, "get_order_by_mp_payment", fake_get_order)
    monkeypatch.setattr(repo, "mark_order_paid", fake_mark_paid)
    monkeypatch.setattr(runner, "get_payment_provider", lambda: _P())
    monkeypatch.setattr(runner, "_deliver_for_order", fake_deliver)
    return rec


def test_delivers_when_paid_and_amount_ok(monkeypatch):
    order = {"id": "o1", "conversation_id": "c1", "status": "pending"}
    rec = _wire_runner(
        monkeypatch, order=order,
        check_result={"paid": True, "amount_cents": 2990}, mark_returns=order,
    )
    asyncio.run(runner.on_infinitepay_payment("nsu", "tx", "slug"))
    assert rec["delivered"] == [order]
    assert rec["mark_calls"] == [("nsu", "tx")]


def test_skips_when_underpaid(monkeypatch):
    order = {"id": "o1", "conversation_id": "c1", "status": "pending"}
    rec = _wire_runner(
        monkeypatch, order=order,
        check_result={"paid": True, "amount_cents": 100}, mark_returns=order,
    )
    asyncio.run(runner.on_infinitepay_payment("nsu", "tx", "slug"))
    assert rec["delivered"] == []
    assert rec["mark_calls"] == []   # never even flips the order


def test_skips_when_not_paid(monkeypatch):
    order = {"id": "o1", "conversation_id": "c1", "status": "pending"}
    rec = _wire_runner(
        monkeypatch, order=order,
        check_result={"paid": False, "amount_cents": 0}, mark_returns=order,
    )
    asyncio.run(runner.on_infinitepay_payment("nsu", "tx", "slug"))
    assert rec["delivered"] == []
    assert rec["mark_calls"] == []


def test_idempotent_when_already_paid(monkeypatch):
    order = {"id": "o1", "conversation_id": "c1", "status": "paid"}
    rec = _wire_runner(
        monkeypatch, order=order,
        check_result={"paid": True, "amount_cents": 2990}, mark_returns=order,
    )
    asyncio.run(runner.on_infinitepay_payment("nsu", "tx", "slug"))
    assert rec["delivered"] == []
    assert rec["check_calls"] == 0   # short-circuits before re-confirming


def test_race_loser_does_not_deliver(monkeypatch):
    order = {"id": "o1", "conversation_id": "c1", "status": "pending"}
    rec = _wire_runner(
        monkeypatch, order=order,
        check_result={"paid": True, "amount_cents": 2990}, mark_returns=None,  # lost the flip
    )
    asyncio.run(runner.on_infinitepay_payment("nsu", "tx", "slug"))
    assert rec["delivered"] == []
