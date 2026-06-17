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

    # Mercado Pago
    mp_access_token: str = ""
    mp_webhook_secret: str = ""

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
