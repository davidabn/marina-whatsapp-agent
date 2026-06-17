"""Shared KIE.ai / Suno request + result contracts.

Mirrors music-pipeline/prompts/*.json exactly. Both the songwriter (which builds
it) and the KIE client (which submits it) depend on this model, so it lives in
the foundation.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class KiePayload(BaseModel):
    """The JSON body POSTed to KIE /api/v1/generate (camelCase on the wire)."""
    model_config = ConfigDict(populate_by_name=True)

    custom_mode: bool = Field(True, alias="customMode")
    instrumental: bool = False
    model: Literal["V5"] = "V5"
    title: str
    style: str
    vocal_gender: Literal["m", "f"] = Field(..., alias="vocalGender")
    style_weight: float = Field(0.75, alias="styleWeight", ge=0.0, le=1.0)
    audio_weight: float = Field(0.70, alias="audioWeight", ge=0.0, le=1.0)
    negative_tags: str = Field("", alias="negativeTags")
    prompt: str  # full lyrics with [Chorus]/[Verse] markers, pt-BR WITHOUT accents
    call_back_url: Optional[str] = Field(None, alias="callBackUrl")

    def to_kie_json(self) -> dict:
        """Serialize with camelCase keys for the API."""
        return self.model_dump(by_alias=True, exclude_none=True)


class Variant(BaseModel):
    id: str
    audio_url: str
    title: str = ""


class GenerationResult(BaseModel):
    task_id: str
    status: str
    variants: list[Variant] = Field(default_factory=list)
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCESS" and bool(self.variants)
