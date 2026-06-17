"""Mercado Pago implementation of `PaymentProvider` (dynamic PIX).

Talks to the Mercado Pago REST API with httpx, authenticating with
`Authorization: Bearer {settings.mp_access_token}`.

Exact JSON paths used (response of POST/GET /v1/payments):
    id                                              -> PixCharge.payment_id (str)
    status                                          -> PixCharge.status
    transaction_amount                              -> PixCharge.amount_cents (*100)
    point_of_interaction.transaction_data.qr_code        -> PixCharge.copia_cola
    point_of_interaction.transaction_data.qr_code_base64 -> PixCharge.qr_base64

Webhook signature (verify_webhook):
    MP sends two headers:
        x-signature:  "ts=<ts>,v1=<hex-hmac>"
        x-request-id: "<uuid>"
    The signed manifest is built as (segments only when their value exists):
        "id:{data.id};request-id:{x-request-id};ts:{ts};"
    where `data.id` is the resource id from the notification body
    (`body.data.id`), lowercased per MP's rule for alphanumeric ids.
    v1 = HMAC_SHA256(manifest, key=settings.mp_webhook_secret).hexdigest()
    Comparison is constant-time. If `mp_webhook_secret` is empty we return True
    (DEV MODE — signatures are NOT verified; set the secret in production).

Webhook body shapes handled (parse_webhook / _extract_payment_id):
    {"type":"payment","action":"payment.updated","data":{"id":"123"}}  -> "123"
    {"data":{"id":123}}                                                -> "123"
    {"data.id":"123"}            (query-style flattened)               -> "123"
    {"id":"123"}                                                       -> "123"
    {"resource":".../v1/payments/123","topic":"payment"}              -> "123"
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from app.config import settings
from app.payments.base import PaymentProvider, PixCharge

_API_BASE = "https://api.mercadopago.com"
_DEFAULT_PAYER_EMAIL = "comprador@musicai.app"


class MercadoPagoProvider(PaymentProvider):
    """PIX charges via Mercado Pago's /v1/payments endpoint."""

    def __init__(
        self,
        access_token: str | None = None,
        webhook_secret: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        # Fall back to settings, but allow injection for tests / multi-account.
        self._access_token = access_token if access_token is not None else settings.mp_access_token
        self._webhook_secret = (
            webhook_secret if webhook_secret is not None else settings.mp_webhook_secret
        )
        self._client = client
        self._timeout = timeout

    # ------------------------------------------------------------------ HTTP
    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue a request reusing an injected client, or an ephemeral one."""
        if self._client is not None:
            resp = await self._client.request(method, url, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    # --------------------------------------------------------------- charges
    async def create_pix_charge(
        self,
        amount_cents: int,
        description: str,
        external_ref: str,
        payer_email: str | None = None,
    ) -> PixCharge:
        body = {
            "transaction_amount": amount_cents / 100,
            "description": description,
            "payment_method_id": "pix",
            "external_reference": external_ref,
            "payer": {"email": payer_email or _DEFAULT_PAYER_EMAIL},
        }
        # X-Idempotency-Key makes retries with the same external_ref safe (MP
        # returns the original charge instead of creating a duplicate).
        headers = {**self._auth_headers, "X-Idempotency-Key": external_ref}
        resp = await self._request(
            "POST", f"{_API_BASE}/v1/payments", json=body, headers=headers
        )
        return self._to_charge(resp.json())

    async def get_payment(self, payment_id: str) -> PixCharge:
        resp = await self._request(
            "GET", f"{_API_BASE}/v1/payments/{payment_id}", headers=self._auth_headers
        )
        return self._to_charge(resp.json())

    @staticmethod
    def _to_charge(data: dict[str, Any]) -> PixCharge:
        """Map a Mercado Pago payment object to a PixCharge."""
        poi = data.get("point_of_interaction") or {}
        txd = poi.get("transaction_data") or {}
        amount = data.get("transaction_amount") or 0
        return PixCharge(
            payment_id=str(data.get("id", "")),
            copia_cola=txd.get("qr_code") or "",
            qr_base64=txd.get("qr_code_base64"),
            status=data.get("status") or "",
            amount_cents=int(round(float(amount) * 100)),
            raw=data,
        )

    # -------------------------------------------------------------- webhooks
    async def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        secret = self._webhook_secret
        if not secret:
            # DEV MODE: no secret configured -> accept everything. Set
            # MP_WEBHOOK_SECRET in production so signatures are enforced.
            return True

        signature = self._header(headers, "x-signature")
        request_id = self._header(headers, "x-request-id")
        if not signature:
            return False

        ts, v1 = self._parse_signature(signature)
        if not v1:
            return False

        data_id = self._extract_payment_id(self._safe_json(raw_body))
        if data_id is not None:
            # MP lowercases alphanumeric ids before signing.
            data_id = data_id.lower()

        manifest = ""
        if data_id is not None:
            manifest += f"id:{data_id};"
        if request_id is not None:
            manifest += f"request-id:{request_id};"
        if ts is not None:
            manifest += f"ts:{ts};"

        expected = hmac.new(
            secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, v1)

    async def parse_webhook(self, headers: dict, body: dict) -> str | None:
        return self._extract_payment_id(body)

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _extract_payment_id(body: Any) -> str | None:
        """Pull the referenced payment id out of MP's various body shapes."""
        if not isinstance(body, dict):
            return None

        data = body.get("data")
        if isinstance(data, dict):
            pid = data.get("id")
            if pid not in (None, ""):
                return str(pid)

        # Query-style flattened key `data.id`.
        flat = body.get("data.id")
        if flat not in (None, ""):
            return str(flat)

        # Plain top-level id.
        pid = body.get("id")
        if pid not in (None, ""):
            return str(pid)

        # `resource` may be a full URL ending in the id.
        resource = body.get("resource")
        if isinstance(resource, str) and resource:
            return resource.rstrip("/").split("/")[-1]

        return None

    @staticmethod
    def _safe_json(raw_body: bytes) -> Any:
        try:
            return json.loads(raw_body)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_signature(signature: str) -> tuple[str | None, str | None]:
        """Parse `ts=<ts>,v1=<hash>` into (ts, v1)."""
        ts: str | None = None
        v1: str | None = None
        for part in signature.split(","):
            key, _, value = part.strip().partition("=")
            key = key.strip()
            value = value.strip()
            if key == "ts":
                ts = value
            elif key == "v1":
                v1 = value
        return ts, v1

    @staticmethod
    def _header(headers: dict, name: str) -> str | None:
        """Case-insensitive header lookup (httpx/Starlette headers vary)."""
        target = name.lower()
        for key, value in headers.items():
            if str(key).lower() == target:
                return value
        return None
