"""Centralized settings loaded from environment (.env).

All modules import the singleton `settings` from here. Do NOT read os.environ
directly elsewhere — add a field here instead so the contract stays in one place.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    public_base_url: str = ""
    price_cents: int = 2990
    tz: str = "America/Sao_Paulo"
    log_level: str = "INFO"
    debounce_seconds: float = 4.0
    business_hours_start: int = 8
    business_hours_end: int = 21
    max_free_regenerations: int = 1

    # Admin / testing
    admin_token: str = ""        # gate for POST /admin/reset-contact (empty => endpoint 401s)
    debug_reset_word: str = ""   # secret WhatsApp word that wipes that chat (empty => disabled)

    # Outbound pacing (humanized typing). Tune via env without code changes.
    typing_min_seconds: float = 2.5
    typing_max_seconds: float = 8.0
    typing_per_char: float = 0.05
    typing_think_min: float = 0.8
    typing_think_max: float = 2.2
    typing_jitter: float = 0.35           # ±35% on the typing component
    audio_pre_delay_min: float = 1.8      # "recording…" pause before each audio
    audio_pre_delay_max: float = 4.0

    # OpenAI (LLM + STT)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_songwriter_model: str = "gpt-4o-mini"
    openai_transcribe_model: str = "gpt-4o-transcribe"

    # KIE.ai / Suno
    kie_api_key: str = ""
    kie_base_url: str = "https://api.kie.ai"
    kie_callback_url: str = ""
    kie_poll_interval: float = 12.0
    kie_max_attempts: int = 30

    # Evolution API (WhatsApp)
    evolution_base_url: str = ""
    evolution_api_key: str = ""
    evolution_instance: str = ""
    evolution_webhook_token: str = ""

    # Payments — which provider the offer/checkout uses ("infinitepay" | "mercadopago")
    payment_provider: str = "infinitepay"

    # Mercado Pago (legacy PIX path — kept dormant)
    mp_access_token: str = ""
    mp_webhook_secret: str = ""

    # InfinitePay Checkout — the only credential is the InfiniteTag (handle, no '$').
    # There is no API key; the public webhook is re-confirmed via /payment_check.
    infinitepay_handle: str = ""
    infinitepay_api_base: str = "https://api.checkout.infinitepay.io"
    infinitepay_redirect_url: str = ""   # where the payer lands after paying (empty => none sent)

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_db_url: str = ""
    supabase_storage_bucket: str = "marina-media"

    # EasyPanel
    easypanel_api_url: str = ""
    easypanel_api_token: str = ""

    @property
    def price_reais(self) -> str:
        """Formatted price, e.g. '29,90'."""
        return f"{self.price_cents / 100:.2f}".replace(".", ",")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
