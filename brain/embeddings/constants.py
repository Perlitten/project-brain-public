"""Embedding dimension constants without database imports."""

from __future__ import annotations

from typing import Optional

from brain.config.settings import settings

_PROVIDER_DIMENSIONS: dict[str, int] = {
    "mock": 1536,
    "openai": 1536,
    "anthropic": 1536,
    "google": 768,
    "nvidia": 4096,
}


def resolve_provider_name(provider_name: Optional[str] = None) -> str:
    return (provider_name or settings.DEFAULT_EMBEDDING_PROVIDER).lower()


def resolve_embedding_dimension(provider_name: Optional[str] = None) -> int:
    if settings.EMBEDDING_DIMENSION > 0:
        return settings.EMBEDDING_DIMENSION
    return _PROVIDER_DIMENSIONS.get(resolve_provider_name(provider_name), 1536)


EMBEDDING_DIMENSION = resolve_embedding_dimension()


class VectorSearchStatus:
    OK = "OK"
    DEGRADED = "DEGRADED"
    DIMENSION_MISMATCH = "DIMENSION_MISMATCH"
    PGVECTOR_UNAVAILABLE = "PGVECTOR_UNAVAILABLE"
