"""Provider-agnostic payments contract (PIX charges).

This module defines the stable interface every payment provider must implement,
so the rest of the app (graph nodes, webhooks, db) depends on `PaymentProvider`
and `PixCharge` rather than on Mercado Pago directly. Swapping in Kiwify /
InfinityPay later means writing a new `PaymentProvider` subclass and nothing
else changes upstream.

Foundation type — keep it free of provider-specific details.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PixCharge:
    """Normalized PIX charge, decoupled from any provider's wire format.

    Fields:
        payment_id:  provider payment id, always stringified.
        copia_cola:  the PIX "copia e cola" / qr_code text the user pastes.
        qr_base64:   base64 PNG of the QR image, or None if the provider
                     did not return one.
        status:      provider-native status string (e.g. MP: "pending",
                     "approved", "rejected", "cancelled"). Use
                     `PaymentProvider.is_approved()` to interpret it.
        amount_cents: charge amount in integer cents (BRL centavos).
        raw:         the full untouched provider response, for debugging /
                     auditing / forward-compat.
    """

    payment_id: str
    copia_cola: str
    qr_base64: str | None
    status: str
    amount_cents: int
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(ABC):
    """Async interface for dynamic PIX charges.

    Network methods (`create_pix_charge`, `get_payment`) and the
    webhook helpers (`verify_webhook`, `parse_webhook`) are async so a single
    contract works for any provider, including ones that must do I/O to verify a
    signature. `is_approved` is a pure-compute helper with a sensible default.
    """

    @abstractmethod
    async def create_pix_charge(
        self,
        amount_cents: int,
        description: str,
        external_ref: str,
        payer_email: str | None = None,
    ) -> PixCharge:
        """Create a dynamic PIX charge and return it (with copia-e-cola + QR)."""
        raise NotImplementedError

    @abstractmethod
    async def get_payment(self, payment_id: str) -> PixCharge:
        """Fetch current state of a payment (poll fallback for webhooks)."""
        raise NotImplementedError

    @abstractmethod
    async def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        """Authenticate an inbound webhook (signature check). True if trusted."""
        raise NotImplementedError

    @abstractmethod
    async def parse_webhook(self, headers: dict, body: dict) -> str | None:
        """Extract the payment_id a notification refers to, or None.

        The caller is expected to then call `get_payment(payment_id)` to confirm
        the authoritative status — webhook bodies are notifications, not state.
        """
        raise NotImplementedError

    async def is_approved(self, status: str) -> bool:
        """True when `status` means the payment cleared.

        Default treats MP-style "approved" (case-insensitive) as paid. Override
        for providers that use different terminal-success terminology.
        """
        return (status or "").strip().lower() == "approved"
