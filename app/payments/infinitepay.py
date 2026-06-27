"""InfinitePay Checkout implementation of `PaymentProvider`.

Unlike Mercado Pago (dynamic PIX copia-e-cola), InfinitePay hands the customer a
hosted **checkout link**. We create the link up front with an `order_nsu` we
generate ourselves (so the webhook can be matched back to our order), then when
the customer pays InfinitePay POSTs a notification to our `webhook_url`.

Endpoints (https://api.checkout.infinitepay.io):
    POST /links          {handle, items:[{quantity,price(cents),description}],
                          order_nsu, redirect_url, webhook_url, customer}
                         -> {"url": "https://checkout.infinitepay.com.br/<handle>?lenc=..."}
    POST /payment_check  {handle, order_nsu, transaction_nsu, slug}
                         -> {"success":..., "paid": bool, "amount":..., "paid_amount":...}

Auth: the ONLY credential is the public `handle` (InfiniteTag, without the `$`).
There is NO API key and NO webhook signature — so the webhook is treated as an
untrusted *notification* and re-confirmed server-side via /payment_check before
anything is delivered (see runner.on_infinitepay_payment).

Amounts are always integer cents (R$ 29,90 -> 2990), matching `price_cents`.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.payments.base import PaymentProvider, PixCharge

logger = logging.getLogger(__name__)


class InfinityPayProvider(PaymentProvider):
    """Checkout links via InfinitePay's /links + /payment_check endpoints."""

    def __init__(
        self,
        handle: str | None = None,
        *,
        api_base: str | None = None,
        redirect_url: str | None = None,
        webhook_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._handle = handle if handle is not None else settings.infinitepay_handle
        self._api_base = (api_base or settings.infinitepay_api_base).rstrip("/")
        self._redirect_url = (
            redirect_url if redirect_url is not None else settings.infinitepay_redirect_url
        )
        # Default the webhook to our public route; allow override for tests.
        if webhook_url is not None:
            self._webhook_url = webhook_url
        elif settings.public_base_url:
            self._webhook_url = settings.public_base_url.rstrip("/") + "/webhooks/infinitepay"
        else:
            self._webhook_url = ""
        self._client = client
        self._timeout = timeout

    # ------------------------------------------------------------------ HTTP
    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._api_base}{path}"
        if self._client is not None:
            resp = await self._client.post(url, json=body)
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {}

    # --------------------------------------------------------------- charges
    async def create_pix_charge(
        self,
        amount_cents: int,
        description: str,
        external_ref: str,
        payer_email: str | None = None,
        *,
        customer: dict[str, Any] | None = None,
    ) -> PixCharge:
        """Create a hosted checkout link; `external_ref` becomes the order_nsu."""
        if not self._handle:
            raise RuntimeError("infinitepay_handle is not configured (InfiniteTag)")

        body: dict[str, Any] = {
            "handle": self._handle,
            "order_nsu": external_ref,
            "items": [
                {"quantity": 1, "price": int(amount_cents), "description": description}
            ],
        }
        if self._webhook_url:
            body["webhook_url"] = self._webhook_url
        if self._redirect_url:
            body["redirect_url"] = self._redirect_url
        if customer:
            # forward only the keys InfinitePay understands
            cust = {
                k: v
                for k, v in customer.items()
                if k in ("name", "email", "phone_number") and v
            }
            if cust:
                body["customer"] = cust

        data = await self._post("/links", body)
        url = self._extract_url(data)
        return PixCharge(
            payment_id=str(external_ref),
            copia_cola=url,            # nothing to "paste" — mirror the link
            qr_base64=None,
            status="pending",
            amount_cents=int(amount_cents),
            checkout_url=url,
            raw=data if isinstance(data, dict) else {},
        )

    async def get_payment(self, payment_id: str) -> PixCharge:
        # InfinitePay status needs the trio (order_nsu, transaction_nsu, slug)
        # from the webhook — there is no single-id lookup. Use payment_check().
        raise NotImplementedError("InfinitePay: use payment_check(order_nsu, transaction_nsu, slug)")

    async def payment_check(
        self, order_nsu: str, transaction_nsu: str | None, slug: str | None
    ) -> dict[str, Any]:
        """Authoritatively confirm a payment. Returns {paid, amount_cents, raw}.

        This is the real authentication for InfinitePay (the webhook is unsigned),
        so the caller MUST gate delivery on `paid` (and ideally the amount).
        """
        if not self._handle:
            raise RuntimeError("infinitepay_handle is not configured (InfiniteTag)")
        body: dict[str, Any] = {"handle": self._handle, "order_nsu": order_nsu}
        if transaction_nsu:
            body["transaction_nsu"] = transaction_nsu
        if slug:
            body["slug"] = slug
        data = await self._post("/payment_check", body)
        paid = bool(data.get("paid")) if isinstance(data, dict) else False
        amount = 0
        if isinstance(data, dict):
            amount = data.get("paid_amount") or data.get("amount") or 0
        return {"paid": paid, "amount_cents": int(amount or 0), "raw": data}

    # -------------------------------------------------------------- webhooks
    async def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        # InfinitePay does not sign webhooks; authenticity is established by the
        # /payment_check re-confirmation in the runner, not here.
        return True

    async def parse_webhook(self, headers: dict, body: dict) -> str | None:
        if not isinstance(body, dict):
            return None
        nsu = body.get("order_nsu")
        return str(nsu) if nsu not in (None, "") else None

    async def is_approved(self, status: str) -> bool:
        return (status or "").strip().lower() == "paid"

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _extract_url(data: Any) -> str:
        """Pull the checkout URL out of the /links response."""
        if isinstance(data, dict):
            url = data.get("url")
            if url:
                return str(url)
            nested = data.get("data")
            if isinstance(nested, dict) and nested.get("url"):
                return str(nested["url"])
        logger.warning("infinitepay /links returned no url: %s", data)
        return ""
