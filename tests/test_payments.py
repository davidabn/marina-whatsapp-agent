"""Logic-only tests for the payments contract (no network).

Covers the pure / I/O-free surface of `MercadoPagoProvider`:

  * `verify_webhook` HMAC-SHA256 signature check (valid / tampered / dev-mode).
  * `parse_webhook` payment-id extraction.
  * `is_approved` status interpretation.

The HMAC manifest is reconstructed here exactly as
`app.payments.mercadopago.MercadoPagoProvider.verify_webhook` builds it:

    id:{data.id};request-id:{x-request-id};ts:{ts};

where `data.id` is `body["data"]["id"]` lowercased. If this mirror ever drifts
from the implementation, the "valid signature" test fails loudly.

These tests use synchronous `def test_*` wrappers that drive the async methods
with `asyncio.run(...)`, matching the repo convention (see test_lyrics.py) so
they run under pytest with no pytest-asyncio plugin, and also standalone via
`python3 tests/test_payments.py`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys

# Make `app` importable whether run via pytest (rootdir) or as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.payments.base import PaymentProvider, PixCharge  # noqa: E402
from app.payments.mercadopago import MercadoPagoProvider  # noqa: E402

_SECRET = "super-secret-test-key"


def _sign(secret: str, data_id: str, request_id: str, ts: str) -> str:
    """Build MP's signed manifest and return the hex HMAC-SHA256 (the `v1`).

    Mirrors MercadoPagoProvider.verify_webhook exactly. `data_id` must already
    be lowercased (MP lowercases alphanumeric ids before signing).
    """
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    return hmac.new(
        secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _webhook_inputs(secret: str, data_id: str, request_id: str, ts: str, v1: str):
    """Return (headers, raw_body) shaped like a real MP webhook delivery."""
    headers = {
        "x-signature": f"ts={ts},v1={v1}",
        "x-request-id": request_id,
    }
    raw_body = json.dumps({"type": "payment", "data": {"id": data_id}}).encode("utf-8")
    return headers, raw_body


# --------------------------------------------------------------------------- #
# verify_webhook (HMAC)
# --------------------------------------------------------------------------- #
def test_verify_webhook_valid_signature():
    data_id, request_id, ts = "123", "req-abc-1", "1700000000"
    v1 = _sign(_SECRET, data_id, request_id, ts)
    headers, raw_body = _webhook_inputs(_SECRET, data_id, request_id, ts, v1)

    provider = MercadoPagoProvider(webhook_secret=_SECRET)
    assert asyncio.run(provider.verify_webhook(headers, raw_body)) is True


def test_verify_webhook_tampered_signature():
    data_id, request_id, ts = "123", "req-abc-1", "1700000000"
    bad_v1 = "0" * 64  # well-formed hex, wrong value
    headers, raw_body = _webhook_inputs(_SECRET, data_id, request_id, ts, bad_v1)

    provider = MercadoPagoProvider(webhook_secret=_SECRET)
    assert asyncio.run(provider.verify_webhook(headers, raw_body)) is False


def test_verify_webhook_empty_secret_is_dev_mode_true():
    # No configured secret -> dev mode -> accept everything, signature ignored.
    provider = MercadoPagoProvider(webhook_secret="")
    assert asyncio.run(provider.verify_webhook({}, b"{}")) is True
    # Even an outright bogus signature is accepted in dev mode.
    headers = {"x-signature": "ts=1,v1=deadbeef", "x-request-id": "x"}
    assert asyncio.run(provider.verify_webhook(headers, b'{"data":{"id":"9"}}')) is True


def test_verify_webhook_lowercases_alphanumeric_id():
    # MP lowercases alphanumeric ids before signing; verify must match.
    raw_id, request_id, ts = "ABC123", "req-z", "1700000001"
    v1 = _sign(_SECRET, raw_id.lower(), request_id, ts)
    headers = {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}
    raw_body = json.dumps({"type": "payment", "data": {"id": raw_id}}).encode("utf-8")

    provider = MercadoPagoProvider(webhook_secret=_SECRET)
    assert asyncio.run(provider.verify_webhook(headers, raw_body)) is True


def test_verify_webhook_missing_signature_header_false():
    provider = MercadoPagoProvider(webhook_secret=_SECRET)
    assert asyncio.run(provider.verify_webhook({}, b'{"data":{"id":"1"}}')) is False


# --------------------------------------------------------------------------- #
# parse_webhook
# --------------------------------------------------------------------------- #
def test_parse_webhook_extracts_id():
    provider = MercadoPagoProvider(webhook_secret=_SECRET)
    body = {"type": "payment", "data": {"id": "123"}}
    assert asyncio.run(provider.parse_webhook({}, body)) == "123"


def test_parse_webhook_returns_none_when_absent():
    provider = MercadoPagoProvider(webhook_secret=_SECRET)
    assert asyncio.run(provider.parse_webhook({}, {"type": "payment"})) is None


# --------------------------------------------------------------------------- #
# is_approved
# --------------------------------------------------------------------------- #
def test_is_approved_true_for_approved():
    provider = MercadoPagoProvider(webhook_secret=_SECRET)
    assert asyncio.run(provider.is_approved("approved")) is True


def test_is_approved_false_for_non_approved():
    provider = MercadoPagoProvider(webhook_secret=_SECRET)
    assert asyncio.run(provider.is_approved("pending")) is False
    assert asyncio.run(provider.is_approved("rejected")) is False


# --------------------------------------------------------------------------- #
# Contract sanity (cheap structural assertions, no network)
# --------------------------------------------------------------------------- #
def test_pixcharge_fields_and_provider_subclass():
    charge = PixCharge(
        payment_id="1",
        copia_cola="000201...",
        qr_base64=None,
        status="pending",
        amount_cents=2990,
    )
    assert charge.payment_id == "1"
    assert charge.qr_base64 is None
    assert charge.amount_cents == 2990
    assert charge.raw == {}  # default_factory
    assert issubclass(MercadoPagoProvider, PaymentProvider)
    # All abstract methods implemented -> instantiable.
    assert isinstance(MercadoPagoProvider(webhook_secret=""), PaymentProvider)


# --------------------------------------------------------------------------- #
# Standalone runner (pytest not required in this env)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:  # noqa: PERF203
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
