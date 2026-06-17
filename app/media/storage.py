"""Supabase Storage upload over the REST object API (httpx).

PUT bytes to the service-role object endpoint with upsert, then return the
public URL. The bucket is settings.supabase_storage_bucket.
"""
from __future__ import annotations

import uuid

import httpx

from app.config import settings


def build_path(conversation_id: str, *, prefix: str = "previews", ext: str = "mp3") -> str:
    """Unique storage key, e.g. 'previews/<conversation_id>/<uuid>.mp3'."""
    return f"{prefix}/{conversation_id}/{uuid.uuid4().hex}.{ext}"


async def upload(path: str, data: bytes, content_type: str) -> str:
    """Upsert `data` to `<bucket>/<path>` and return its public URL."""
    bucket = settings.supabase_storage_bucket
    base = settings.supabase_url.rstrip("/")
    object_url = f"{base}/storage/v1/object/{bucket}/{path}"
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.put(object_url, content=data, headers=headers)
        resp.raise_for_status()
    return f"{base}/storage/v1/object/public/{bucket}/{path}"
